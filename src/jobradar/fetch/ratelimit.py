"""Per-domain rate limiting.

One token bucket per domain, so a slow host never blocks a fast one. Requests to
different domains proceed in parallel; requests to the same domain queue behind a
single lock and are spaced by the bucket.

Jitter matters: a perfectly regular request every 2.000s looks more like a bot than
a human and is easier to fingerprint and block. We spread each wait by +/-30%.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field


@dataclass
class RateLimitPolicy:
    """How hard we are willing to hit one domain.

    Attributes:
        requests_per_second: Steady-state rate. The default 0.5 means one request
            every two seconds, which is far below what these sites serve to a
            normal browsing user.
        burst: Tokens the bucket may accumulate while idle, letting a run of
            requests start immediately rather than waiting out the first interval.
        jitter: Fractional spread applied to each computed wait, in [0, 1).
    """

    requests_per_second: float = 0.5
    burst: int = 1
    jitter: float = 0.3

    def __post_init__(self) -> None:
        if self.requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        if self.burst < 1:
            raise ValueError("burst must be at least 1")
        if not 0 <= self.jitter < 1:
            raise ValueError("jitter must be in [0, 1)")


@dataclass
class _Bucket:
    policy: RateLimitPolicy
    tokens: float
    updated_at: float
    lock: threading.Lock = field(default_factory=threading.Lock)


class RateLimiter:
    """Token buckets keyed by domain.

    Thread-safe. ``acquire`` blocks until a token is available for that domain.
    """

    def __init__(
        self,
        default_policy: RateLimitPolicy | None = None,
        policies: dict[str, RateLimitPolicy] | None = None,
        *,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ) -> None:
        # sleep/monotonic are injectable so tests can run without real waiting.
        self._default = default_policy or RateLimitPolicy()
        self._policies = dict(policies or {})
        self._buckets: dict[str, _Bucket] = {}
        self._registry_lock = threading.Lock()
        self._sleep = sleep
        self._monotonic = monotonic

    def policy_for(self, domain: str) -> RateLimitPolicy:
        return self._policies.get(domain, self._default)

    def set_policy(self, domain: str, policy: RateLimitPolicy) -> None:
        with self._registry_lock:
            self._policies[domain] = policy
            self._buckets.pop(domain, None)

    def _bucket(self, domain: str) -> _Bucket:
        with self._registry_lock:
            bucket = self._buckets.get(domain)
            if bucket is None:
                policy = self.policy_for(domain)
                bucket = _Bucket(
                    policy=policy,
                    tokens=float(policy.burst),
                    updated_at=self._monotonic(),
                )
                self._buckets[domain] = bucket
            return bucket

    def acquire(self, domain: str) -> float:
        """Block until a request to ``domain`` may proceed.

        Returns the number of seconds spent waiting, which callers log so a run's
        politeness overhead is visible rather than invisible.
        """
        bucket = self._bucket(domain)
        # Held for the whole refill-and-wait so two threads cannot both consume
        # the same token and double the effective rate.
        with bucket.lock:
            policy = bucket.policy
            now = self._monotonic()
            elapsed = now - bucket.updated_at
            bucket.tokens = min(
                float(policy.burst), bucket.tokens + elapsed * policy.requests_per_second
            )
            bucket.updated_at = now

            waited = 0.0
            if bucket.tokens < 1.0:
                deficit = 1.0 - bucket.tokens
                wait = deficit / policy.requests_per_second
                if policy.jitter:
                    wait *= 1.0 + random.uniform(-policy.jitter, policy.jitter)
                wait = max(0.0, wait)
                self._sleep(wait)
                waited = wait
                bucket.updated_at = self._monotonic()
                bucket.tokens = 1.0

            bucket.tokens -= 1.0
            return waited
