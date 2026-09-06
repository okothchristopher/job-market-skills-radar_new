"""PoliteClient tests — the composition of robots, rate limiting, cache and retry."""

from __future__ import annotations

import pytest

from jobradar.fetch.cache import ResponseCache
from jobradar.fetch.client import FetchError, PoliteClient
from jobradar.fetch.ratelimit import RateLimiter, RateLimitPolicy
from jobradar.fetch.robots import RobotsDisallowed, RobotsGate


class FakeHTTPResponse:
    def __init__(self, status_code=200, content=b"ok", headers=None, encoding="utf-8"):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.encoding = encoding

    @property
    def text(self):
        return self.content.decode(self.encoding or "utf-8")


class ScriptedSession:
    """Returns queued responses in order and records the URLs requested."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        if not self.responses:
            return FakeHTTPResponse()
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def build(responses, *, cache=None, respect_robots=False, max_retries=3):
    session = ScriptedSession(responses)
    client = PoliteClient(
        cache=cache,
        limiter=RateLimiter(default_policy=RateLimitPolicy(requests_per_second=1000.0, jitter=0.0)),
        session=session,
        respect_robots=respect_robots,
        max_retries=max_retries,
        sleep=lambda s: None,  # no real backoff waits in tests
    )
    return client, session


def test_successful_fetch():
    client, session = build([FakeHTTPResponse(200, b"<html>job</html>")])
    response = client.get("https://example.com/job/1")
    assert response.status_code == 200
    assert response.text == "<html>job</html>"
    assert response.from_cache is False
    assert client.stats.requests == 1


def test_cache_hit_skips_the_network(tmp_path):
    cache = ResponseCache(tmp_path / "c.sqlite")
    client, session = build([FakeHTTPResponse(200, b"first")], cache=cache)

    client.get("https://example.com/a")
    second = client.get("https://example.com/a")

    assert second.from_cache is True
    assert second.text == "first"
    assert len(session.calls) == 1, "a cache hit must not touch the network"
    assert client.stats.cache_hits == 1
    cache.close()


def test_robots_blocks_before_any_request():
    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, headers=None, timeout=None):
            self.calls.append(url)
            if url.endswith("robots.txt"):
                return FakeHTTPResponse(200, b"User-agent: *\nDisallow: /private\n")
            return FakeHTTPResponse(200, b"should never be reached")

    session = Session()
    client = PoliteClient(
        limiter=RateLimiter(default_policy=RateLimitPolicy(1000.0, jitter=0.0)),
        robots=RobotsGate("TestBot/1.0", session=session),
        session=session,
        respect_robots=True,
        sleep=lambda s: None,
    )

    with pytest.raises(RobotsDisallowed):
        client.get("https://example.com/private/thing")

    assert not any("private" in c for c in session.calls), "disallowed URL was fetched anyway"
    assert client.stats.robots_blocked == 1


def test_retries_on_429_then_succeeds():
    client, session = build(
        [
            FakeHTTPResponse(429, b"slow down", {"Retry-After": "1"}),
            FakeHTTPResponse(200, b"finally"),
        ]
    )
    response = client.get("https://example.com/a")
    assert response.text == "finally"
    assert client.stats.retries == 1


def test_retries_on_503_then_gives_up():
    client, _ = build([FakeHTTPResponse(503, b"down")] * 5, max_retries=2)
    with pytest.raises(FetchError) as exc:
        client.get("https://example.com/a")
    assert exc.value.status_code == 503
    assert exc.value.attempts == 3
    assert client.stats.failures == 1


def test_404_is_not_retried():
    """A missing page will not appear by asking again."""
    client, session = build([FakeHTTPResponse(404, b"gone")])
    with pytest.raises(FetchError):
        client.get("https://example.com/missing")
    assert len(session.calls) == 1
    assert client.stats.retries == 0


def test_network_errors_are_retried():
    client, _ = build([ConnectionError("reset"), FakeHTTPResponse(200, b"recovered")])
    assert client.get("https://example.com/a").text == "recovered"


def test_failures_raise_rather_than_returning_zero():
    """The previous scraper swallowed failures into zeroes, which is
    indistinguishable from a skill genuinely not appearing."""
    client, _ = build([FakeHTTPResponse(500, b"boom")] * 10, max_retries=1)
    with pytest.raises(FetchError):
        client.get("https://example.com/a")


def test_user_agent_identifies_the_project():
    client, session = build([FakeHTTPResponse()])
    captured = {}

    def capture(url, headers=None, timeout=None):
        captured.update(headers or {})
        return FakeHTTPResponse()

    session.get = capture
    client.get("https://example.com/a")
    assert "ZinduaJobRadar" in captured["User-Agent"]
    assert "zinduaschool.com" in captured["User-Agent"]


def test_rate_limiting_is_applied_per_domain():
    client, _ = build([FakeHTTPResponse()] * 4)
    client.limiter = RateLimiter(
        default_policy=RateLimitPolicy(requests_per_second=1.0, jitter=0.0),
        sleep=lambda s: None,
        monotonic=lambda: 0.0,  # frozen clock, so the bucket never refills
    )
    for i in range(3):
        client.get(f"https://example.com/{i}")
    assert client.stats.seconds_waiting > 0
    assert client.stats.by_domain["example.com"] == 3


# ------------------------------------------------------- robots crawl-delay


class _RobotsSession:
    """Serves a robots.txt with a Crawl-delay, then normal pages."""

    def __init__(self, robots_body):
        self.robots_body = robots_body
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        if url.endswith("robots.txt"):
            return FakeHTTPResponse(200, self.robots_body.encode())
        return FakeHTTPResponse(200, b"page")


def _client_with_robots(robots_body, configured_rate):
    session = _RobotsSession(robots_body)
    return PoliteClient(
        limiter=RateLimiter(
            default_policy=RateLimitPolicy(requests_per_second=configured_rate, jitter=0.0),
            sleep=lambda s: None,  # assert the policy, do not actually wait it out
        ),
        robots=RobotsGate("TestBot/1.0", session=session),
        session=session,
        respect_robots=True,
        sleep=lambda s: None,
    )


def test_declared_crawl_delay_slows_us_down():
    """JobWebKenya declares Crawl-delay: 60, which is 24x slower than our
    default. Reading robots.txt but ignoring the one directive asking us to slow
    down would make the hard-gate claim hollow."""
    client = _client_with_robots("User-agent: *\nCrawl-delay: 60\nDisallow: /admin\n", 0.5)
    client.get("https://slow.example/page")
    assert client.limiter.policy_for("slow.example").requests_per_second == pytest.approx(1 / 60)


def test_permissive_crawl_delay_does_not_speed_us_up():
    """The stricter of the two always wins; a host declaring a short delay does
    not license us to go faster than configured."""
    client = _client_with_robots("User-agent: *\nCrawl-delay: 0.1\n", 0.5)
    client.get("https://fast.example/page")
    assert client.limiter.policy_for("fast.example").requests_per_second == pytest.approx(0.5)


def test_absent_crawl_delay_leaves_policy_alone():
    client = _client_with_robots("User-agent: *\nDisallow: /admin\n", 0.4)
    client.get("https://plain.example/page")
    assert client.limiter.policy_for("plain.example").requests_per_second == pytest.approx(0.4)


def test_crawl_delay_is_applied_once_per_domain():
    client = _client_with_robots("User-agent: *\nCrawl-delay: 5\n", 1.0)
    for i in range(4):
        client.get(f"https://once.example/{i}")
    assert client.limiter.policy_for("once.example").requests_per_second == pytest.approx(0.2)
