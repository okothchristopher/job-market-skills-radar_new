"""The polite HTTP client.

Every network request in this project goes through here. Source adapters never
call ``requests`` directly, which is what makes rate-limit and robots policy
enforceable in one place rather than by convention across a dozen modules.

Order of operations for each request:

1. Cache lookup (a hit costs no network traffic and no rate-limit token)
2. robots.txt gate, which raises on disallow
3. Rate-limit token for that domain
4. Fetch, with retry and backoff on 429/5xx
5. Cache store
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .cache import CachedResponse, ResponseCache
from .ratelimit import RateLimiter, RateLimitPolicy
from .robots import RobotsGate

log = logging.getLogger(__name__)

# We identify ourselves honestly rather than spoofing a rotating browser UA. The
# contact URL gives an operator a way to reach us instead of silently blocking.
DEFAULT_USER_AGENT = (
    "ZinduaJobRadar/0.1 (research crawler for curriculum planning; "
    "+https://zinduaschool.com; contact: data@zinduaschool.com)"
)

RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """Raised when a URL could not be fetched after all retries."""

    def __init__(self, url: str, status_code: int | None, attempts: int, reason: str) -> None:
        super().__init__(f"failed to fetch {url!r} after {attempts} attempt(s): {reason}")
        self.url = url
        self.status_code = status_code
        self.attempts = attempts
        self.reason = reason


@dataclass
class Response:
    """A fetched (or cached) response."""

    url: str
    status_code: int
    headers: dict[str, str]
    body: bytes
    encoding: str | None
    from_cache: bool

    @property
    def text(self) -> str:
        return self.body.decode(self.encoding or "utf-8", errors="replace")

    def json(self):
        import json

        return json.loads(self.text)


@dataclass
class FetchStats:
    """Per-run counters, reported at the end of a crawl so the politeness
    overhead and cache effectiveness are visible rather than guessed at."""

    requests: int = 0
    cache_hits: int = 0
    retries: int = 0
    failures: int = 0
    robots_blocked: int = 0
    seconds_waiting: float = 0.0
    by_domain: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "retries": self.retries,
            "failures": self.failures,
            "robots_blocked": self.robots_blocked,
            "seconds_waiting": round(self.seconds_waiting, 1),
            "by_domain": dict(self.by_domain),
        }


class PoliteClient:
    """HTTP client with robots enforcement, per-domain rate limiting and caching."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        cache: ResponseCache | None = None,
        limiter: RateLimiter | None = None,
        robots: RobotsGate | None = None,
        session=None,
        timeout: float = 30.0,
        max_retries: int = 3,
        respect_robots: bool = True,
        sleep=time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.cache = cache
        self.limiter = limiter or RateLimiter()
        # respect_robots exists for testing against local fixtures only. It is
        # never disabled against a live host.
        self.respect_robots = respect_robots
        self.robots = robots or RobotsGate(user_agent, session=session)
        self.timeout = timeout
        self.max_retries = max_retries
        self.stats = FetchStats()
        # Domains whose robots Crawl-delay we have already applied; the
        # lookup is cached but the policy swap only needs to happen once.
        self._crawl_delay_applied: set[str] = set()
        self._sleep = sleep
        self._session = session

    def _session_or_default(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    @staticmethod
    def _domain(url: str) -> str:
        return urlsplit(url).netloc

    def _apply_crawl_delay(self, domain: str, url: str) -> None:
        """Honour a host-declared Crawl-delay when it is stricter than our config.

        A site asking for more space than we planned to give it gets it. This is
        not cosmetic: JobWebKenya declares ``Crawl-delay: 60``, which is 24x
        slower than our default and completely changes how that source can be
        crawled. Reading robots.txt but ignoring the one directive that asks us
        to slow down would make the whole "hard gate" claim hollow.

        The stricter of the two always wins, and we never speed up to match a
        permissive declaration.
        """
        if domain in self._crawl_delay_applied or not self.respect_robots:
            return
        self._crawl_delay_applied.add(domain)

        try:
            declared = self.robots.crawl_delay(url)
        except Exception:
            return
        if not declared or declared <= 0:
            return

        declared_rate = 1.0 / declared
        current = self.limiter.policy_for(domain)
        if declared_rate < current.requests_per_second:
            log.info(
                "%s declares Crawl-delay: %.0fs; slowing from %.2f to %.4f req/s",
                domain,
                declared,
                current.requests_per_second,
                declared_rate,
            )
            self.limiter.set_policy(
                domain,
                RateLimitPolicy(
                    requests_per_second=declared_rate,
                    burst=1,
                    jitter=current.jitter,
                ),
            )

    def _backoff_seconds(self, attempt: int, retry_after: str | None) -> float:
        """Honour Retry-After when the server sends it, else exponential with jitter."""
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass  # Retry-After may be an HTTP date; fall through to backoff.
        return (2.0**attempt) * random.uniform(0.75, 1.25)

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
    ) -> Response:
        """Fetch ``url``, or raise :class:`FetchError` / ``RobotsDisallowed``."""
        if use_cache and self.cache is not None:
            cached: CachedResponse | None = self.cache.get(url)
            if cached is not None:
                self.stats.cache_hits += 1
                return Response(
                    url=cached.url,
                    status_code=cached.status_code,
                    headers=cached.headers,
                    body=cached.body,
                    encoding=cached.encoding,
                    from_cache=True,
                )

        if self.respect_robots:
            try:
                self.robots.check(url)
            except Exception:
                self.stats.robots_blocked += 1
                raise

        domain = self._domain(url)
        self._apply_crawl_delay(domain, url)
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept-Language": "en-KE,en-GB;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        }
        if headers:
            request_headers.update(headers)

        last_reason = "unknown"
        last_status: int | None = None

        for attempt in range(self.max_retries + 1):
            self.stats.seconds_waiting += self.limiter.acquire(domain)

            try:
                response = self._session_or_default().get(
                    url, headers=request_headers, timeout=self.timeout
                )
            except Exception as exc:  # network-level failure
                last_reason = f"{type(exc).__name__}: {exc}"
                last_status = None
            else:
                self.stats.requests += 1
                self.stats.by_domain[domain] = self.stats.by_domain.get(domain, 0) + 1
                last_status = response.status_code

                if response.status_code < 400:
                    body = response.content
                    if use_cache and self.cache is not None:
                        self.cache.put(
                            url,
                            response.status_code,
                            dict(response.headers),
                            body,
                            response.encoding,
                        )
                    return Response(
                        url=url,
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        body=body,
                        encoding=response.encoding,
                        from_cache=False,
                    )

                if response.status_code not in RETRY_STATUS:
                    # 404 and friends will not improve by asking again.
                    self.stats.failures += 1
                    raise FetchError(url, response.status_code, attempt + 1, "client error")

                last_reason = f"HTTP {response.status_code}"

            if attempt < self.max_retries:
                delay = self._backoff_seconds(
                    attempt,
                    response.headers.get("Retry-After") if last_status else None,
                )
                self.stats.retries += 1
                log.warning(
                    "retrying %s in %.1fs (attempt %d/%d): %s",
                    url,
                    delay,
                    attempt + 1,
                    self.max_retries,
                    last_reason,
                )
                self._sleep(delay)

        self.stats.failures += 1
        raise FetchError(url, last_status, self.max_retries + 1, last_reason)


def build_client(
    *,
    cache_path: str | None = "data/cache/http_cache.sqlite",
    cache_ttl_seconds: float = 14 * 24 * 3600,
    default_rate: float = 0.5,
    domain_rates: dict[str, float] | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
) -> PoliteClient:
    """Construct a client from plain config values."""
    cache = ResponseCache(cache_path, ttl_seconds=cache_ttl_seconds) if cache_path else None
    policies = {
        domain: RateLimitPolicy(requests_per_second=rate)
        for domain, rate in (domain_rates or {}).items()
    }
    limiter = RateLimiter(
        default_policy=RateLimitPolicy(requests_per_second=default_rate),
        policies=policies,
    )
    return PoliteClient(user_agent=user_agent, cache=cache, limiter=limiter)
