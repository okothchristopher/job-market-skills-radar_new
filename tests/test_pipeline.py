"""Pipeline tests: source resolution, queue draining, and failure isolation."""

from __future__ import annotations

import json

import pytest

from jobradar import pipeline
from jobradar.config import Config
from jobradar.fetch.client import PoliteClient
from jobradar.fetch.ratelimit import RateLimiter, RateLimitPolicy
from jobradar.sources import registry
from jobradar.sources.base import SourceAdapter
from jobradar.store.db import JobStore
from jobradar.store.models import GROUP_GLOBAL, RawJob


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.sqlite")
    yield s
    s.close()


@pytest.fixture
def config():
    return Config.load()


# ------------------------------------------------------------ source resolution


def test_group_alias_resolves_to_enabled_adapters(config):
    names = pipeline.resolve_sources("global", config)
    assert "greenhouse" in names and "hn_hiring" in names


def test_disabled_sources_are_excluded(config):
    """Remotive is disabled because its robots.txt disallows its own API path."""
    assert "remotive" not in pipeline.resolve_sources("global", config)


def test_explicit_list_resolves(config):
    assert pipeline.resolve_sources("greenhouse,ashby", config) == ["greenhouse", "ashby"]


def test_unknown_source_raises(config):
    with pytest.raises(KeyError):
        pipeline.resolve_sources("not_a_real_board", config)


def test_empty_spec_returns_nothing(config):
    assert pipeline.resolve_sources("", config) == []


# ---------------------------------------------------------------- drain loop


class _StubAdapter(SourceAdapter):
    """Yields one job per URL, and optionally a follow-on page."""

    name = "stub"
    group = GROUP_GLOBAL

    def __init__(self, client, config=None, follow_to=None):
        super().__init__(client, config)
        self.follow_to = dict(follow_to or {})
        self.parsed: list[str] = []

    def discover(self):
        return iter(["https://stub.example/1"])

    def parse(self, response):
        self.parsed.append(response.url)
        return [
            RawJob(
                source=self.name,
                source_group=self.group,
                native_id=response.url.rsplit("/", 1)[-1],
                url=response.url,
                title="Engineer",
            )
        ]

    def next_urls(self, response):
        follow = self.follow_to.get(response.url)
        return [follow] if follow else []


class _StubSession:
    def __init__(self, fail_urls=()):
        self.fail_urls = set(fail_urls)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)

        class R:
            status_code = 500 if url in self.fail_urls else 200
            content = json.dumps({"ok": True}).encode()
            encoding = "utf-8"

            def __init__(self):
                self.headers = {}

        return R()


def _client(session):
    return PoliteClient(
        cache=None,
        limiter=RateLimiter(default_policy=RateLimitPolicy(1000.0, jitter=0.0)),
        session=session,
        respect_robots=False,
        max_retries=0,
        sleep=lambda s: None,
    )


def _run(store, adapter, config, limit=0):
    """Drive pipeline.fetch with a stub adapter swapped into the registry."""
    original = registry._adapters.get("stub")
    registry._adapters["stub"] = lambda client, cfg: adapter
    try:
        return pipeline.fetch(["stub"], config, store, limit=limit, client=adapter.client)
    finally:
        if original is None:
            registry._adapters.pop("stub", None)
        else:
            registry._adapters["stub"] = original


def test_follow_on_pages_are_drained(store, config):
    """Cursor pagination: page 1 enqueues page 2, which enqueues page 3."""
    session = _StubSession()
    adapter = _StubAdapter(
        _client(session),
        follow_to={
            "https://stub.example/1": "https://stub.example/2",
            "https://stub.example/2": "https://stub.example/3",
        },
    )
    store.enqueue(["https://stub.example/1"], source="stub")

    stats = _run(store, adapter, config)

    assert stats["stub"]["urls"] == 3
    assert stats["stub"]["jobs_stored"] == 3
    assert adapter.parsed == [
        "https://stub.example/1",
        "https://stub.example/2",
        "https://stub.example/3",
    ]


def test_limit_caps_the_drain(store, config):
    session = _StubSession()
    adapter = _StubAdapter(
        _client(session),
        follow_to={
            f"https://stub.example/{i}": f"https://stub.example/{i + 1}" for i in range(1, 9)
        },
    )
    store.enqueue(["https://stub.example/1"], source="stub")

    stats = _run(store, adapter, config, limit=3)
    assert stats["stub"]["urls"] == 3


def test_a_failed_url_does_not_abort_the_crawl(store, config):
    """One bad URL out of thousands must not end the run — and the failure has
    to be recorded rather than silently becoming a zero."""
    session = _StubSession(fail_urls={"https://stub.example/2"})
    adapter = _StubAdapter(
        _client(session),
        follow_to={
            "https://stub.example/1": "https://stub.example/2",
            "https://stub.example/2": "https://stub.example/3",
        },
    )
    store.enqueue(["https://stub.example/1", "https://stub.example/3"], source="stub")

    stats = _run(store, adapter, config)

    assert stats["stub"]["failed"] == 1
    assert stats["stub"]["jobs_stored"] == 2
    assert store.failure_count("stub") == 1


def test_queue_state_persists_for_resumption(store, config):
    session = _StubSession()
    adapter = _StubAdapter(_client(session))
    store.enqueue([f"https://stub.example/{i}" for i in range(1, 6)], source="stub")

    _run(store, adapter, config, limit=2)
    assert store.queue_stats("stub")["done"] == 2
    assert store.queue_stats("stub")["pending"] == 3

    _run(store, adapter, config)
    assert store.queue_stats("stub")["done"] == 5


# ------------------------------------------------------------------ companies


def test_load_companies_returns_empty_when_absent(tmp_path):
    assert pipeline.load_companies(str(tmp_path / "nope.yaml")) == []


def test_confirmed_companies_have_required_fields():
    """companies.yaml drives both ATS adapters; a missing ats field silently
    drops that board from discovery."""
    companies = pipeline.load_companies()
    if not companies:
        pytest.skip("bootstrap has not been run")
    for record in companies:
        assert record.get("slug")
        assert record.get("ats") in {"greenhouse", "ashby"}
        assert record.get("segment")
