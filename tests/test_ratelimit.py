"""Rate limiter tests.

Time is injected rather than real, so these assert the pacing logic exactly
without the suite taking minutes to run.
"""

from __future__ import annotations

import pytest

from jobradar.fetch.ratelimit import RateLimiter, RateLimitPolicy


class FakeClock:
    """A monotonic clock that only advances when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_limiter(rate=1.0, burst=1, jitter=0.0):
    clock = FakeClock()
    limiter = RateLimiter(
        default_policy=RateLimitPolicy(requests_per_second=rate, burst=burst, jitter=jitter),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    return limiter, clock


def test_first_request_does_not_wait():
    limiter, clock = make_limiter(rate=0.5)
    assert limiter.acquire("example.com") == 0.0
    assert clock.sleeps == []


def test_second_request_waits_the_interval():
    limiter, clock = make_limiter(rate=0.5, jitter=0.0)  # one request per 2s
    limiter.acquire("example.com")
    waited = limiter.acquire("example.com")
    assert waited == pytest.approx(2.0)


def test_domains_are_independent():
    """A slow host must not delay a fast one."""
    limiter, clock = make_limiter(rate=0.5)
    limiter.acquire("slow.example")
    limiter.acquire("slow.example")
    waited = limiter.acquire("fast.example")
    assert waited == 0.0, "a fresh domain should have a full bucket"


def test_burst_allows_a_short_run_then_throttles():
    limiter, clock = make_limiter(rate=1.0, burst=3, jitter=0.0)
    assert [limiter.acquire("example.com") for _ in range(3)] == [0.0, 0.0, 0.0]
    assert limiter.acquire("example.com") == pytest.approx(1.0)


def test_tokens_refill_while_idle():
    limiter, clock = make_limiter(rate=1.0, jitter=0.0)
    limiter.acquire("example.com")
    clock.now += 5.0  # idle period
    assert limiter.acquire("example.com") == 0.0


def test_jitter_stays_within_bounds():
    limiter, _ = make_limiter(rate=1.0, jitter=0.3)
    limiter.acquire("example.com")
    waits = []
    for _ in range(30):
        waits.append(limiter.acquire("example.com"))
    assert all(0.7 <= w <= 1.3 for w in waits), waits
    assert len(set(waits)) > 1, "jitter should vary the wait, not fix it"


def test_per_domain_policy_overrides_default():
    clock = FakeClock()
    limiter = RateLimiter(
        default_policy=RateLimitPolicy(requests_per_second=0.5, jitter=0.0),
        policies={"fast.example": RateLimitPolicy(requests_per_second=2.0, jitter=0.0)},
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    limiter.acquire("fast.example")
    assert limiter.acquire("fast.example") == pytest.approx(0.5)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"requests_per_second": 0},
        {"requests_per_second": -1},
        {"burst": 0},
        {"jitter": 1.0},
        {"jitter": -0.1},
    ],
)
def test_invalid_policies_are_rejected(kwargs):
    with pytest.raises(ValueError):
        RateLimitPolicy(**kwargs)
