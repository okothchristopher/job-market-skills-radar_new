"""robots.txt gate tests.

The BrighterMonday fixture below is its real robots.txt as retrieved during
planning. It is the awkward case that motivates having a hard gate at all: search
query strings are disallowed while /listings/ detail pages and pages 2-7 are
explicitly allowed. Pinning it here means a future crawl plan that drifts back
towards keyword-search URLs fails a test rather than getting quietly blocked.
"""

from __future__ import annotations

import pytest

from jobradar.fetch.robots import RobotsDisallowed, RobotsGate

BRIGHTERMONDAY_ROBOTS = """
User-agent: *

Disallow: /admin/
Disallow: /account/
Disallow: /api/
Disallow: /job/
Disallow: /blog
Disallow: /ajax/
Disallow: /report-job/
Disallow: /*q=*
Disallow: /*industry=*
Disallow: /*sort=*
Disallow: /*keywords=*
Disallow: /*job_type=*
Disallow: /*page=*

Allow: /account/login
Allow: *page=2$
Allow: *page=3$
"""

MYJOBMAG_ROBOTS = """
User-agent: *
Disallow: /search/jobs?*
Disallow: /*?
Disallow: /profile
Disallow: /apply-now/
Disallow: /learn/
"""

PERMISSIVE_ROBOTS = "User-agent: *\nDisallow:\n"


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class FakeSession:
    """Serves canned robots.txt bodies keyed by host, and counts fetches."""

    def __init__(self, bodies: dict[str, str], status: int = 200) -> None:
        self.bodies = bodies
        self.status = status
        self.calls: list[str] = []

    def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        for host, body in self.bodies.items():
            if host in url:
                return FakeResponse(body, self.status)
        return FakeResponse("", 404)


def gate_for(body: str, host: str = "www.example.com") -> RobotsGate:
    return RobotsGate("TestBot/1.0", session=FakeSession({host: body}))


# ------------------------------------------------------- BrighterMonday rules


@pytest.mark.parametrize(
    "path",
    [
        "/listings/qa-developer-5p64x6",
        "/jobs/software-data",
        "/jobs/it-telecoms",
    ],
)
def test_brightermonday_allows_listings_and_categories(path):
    gate = gate_for(BRIGHTERMONDAY_ROBOTS, "www.brightermonday.co.ke")
    assert gate.is_allowed(f"https://www.brightermonday.co.ke{path}")


@pytest.mark.parametrize(
    "path",
    [
        "/jobs?q=python",
        "/jobs?keywords=django",
        "/jobs/software-data?page=9",
        "/api/jobs",
        "/job/12345",
    ],
)
def test_brightermonday_blocks_search_and_api(path):
    """These are exactly the URL shapes the previous scraper's approach used."""
    gate = gate_for(BRIGHTERMONDAY_ROBOTS, "www.brightermonday.co.ke")
    assert not gate.is_allowed(f"https://www.brightermonday.co.ke{path}")


# ------------------------------------------------------------ MyJobMag rules


def test_myjobmag_allows_path_pagination_but_not_query_strings():
    gate = gate_for(MYJOBMAG_ROBOTS, "www.myjobmag.co.ke")
    base = "https://www.myjobmag.co.ke"
    assert gate.is_allowed(f"{base}/jobs-by-field/information-technology")
    assert gate.is_allowed(f"{base}/jobs-by-field/information-technology/2")
    assert gate.is_allowed(f"{base}/job/business-analyst-gulf-african-bank-2")
    assert not gate.is_allowed(f"{base}/search/jobs?q=python")


# --------------------------------------------------------------- gate behaviour


def test_check_raises_on_disallowed():
    gate = gate_for(BRIGHTERMONDAY_ROBOTS, "www.brightermonday.co.ke")
    with pytest.raises(RobotsDisallowed):
        gate.check("https://www.brightermonday.co.ke/jobs?q=python")


def test_check_passes_on_allowed():
    gate = gate_for(BRIGHTERMONDAY_ROBOTS, "www.brightermonday.co.ke")
    gate.check("https://www.brightermonday.co.ke/listings/some-job-abc123")


def test_rules_are_cached_per_domain():
    """One robots.txt fetch per host, not one per URL checked."""
    session = FakeSession({"www.example.com": PERMISSIVE_ROBOTS})
    gate = RobotsGate("TestBot/1.0", session=session)
    for i in range(10):
        gate.is_allowed(f"https://www.example.com/page/{i}")
    assert len(session.calls) == 1


def test_missing_robots_fails_open():
    """A 404 conventionally means no restrictions."""
    session = FakeSession({}, status=404)
    gate = RobotsGate("TestBot/1.0", session=session)
    assert gate.is_allowed("https://nowhere.example/anything")


def test_network_error_fails_open():
    class ExplodingSession:
        def get(self, *args, **kwargs):
            raise ConnectionError("no route to host")

    gate = RobotsGate("TestBot/1.0", session=ExplodingSession())
    assert gate.is_allowed("https://unreachable.example/page")


def test_crawl_delay_is_reported_when_declared():
    gate = gate_for("User-agent: *\nCrawl-delay: 5\nDisallow: /private\n")
    assert gate.crawl_delay("https://www.example.com/") == 5.0


# ------------------------------------------------- regression: blank-line bug


def test_blank_line_after_user_agent_does_not_void_the_group():
    """Regression guard for a silent compliance failure.

    Python's stdlib ``urllib.robotparser`` treats a blank line as a group
    terminator, so a file shaped like BrighterMonday's real one — ``User-agent: *``,
    blank line, then the rules — parses to *no rules at all* and reports every URL
    as allowed. The crawl would look polite while ignoring every directive.

    If this test ever fails, the robots parser has regressed to stdlib behaviour.
    """
    body = "User-agent: *\n\nDisallow: /api/\nDisallow: /job/\n"
    gate = gate_for(body)
    assert not gate.is_allowed("https://www.example.com/api/jobs")
    assert not gate.is_allowed("https://www.example.com/job/12345")
    assert gate.is_allowed("https://www.example.com/listings/abc")


def test_allow_overrides_disallow_for_specific_pages():
    """BrighterMonday blocks ?page= generally but allows pages 2-7 explicitly."""
    gate = gate_for(BRIGHTERMONDAY_ROBOTS, "www.brightermonday.co.ke")
    base = "https://www.brightermonday.co.ke/jobs/software-data"
    assert gate.is_allowed(f"{base}?page=2")
    assert not gate.is_allowed(f"{base}?page=9")


def test_sitemaps_are_exposed():
    """Fuzu and MyJobMag advertise job sitemaps here — the cheapest way in."""
    body = (
        "User-agent: *\nDisallow: /admin/\n"
        "Sitemap: https://www.example.com/sitemap-jobs.xml\n"
        "Sitemap: https://www.example.com/sitemap-main.xml\n"
    )
    gate = gate_for(body)
    assert set(gate.sitemaps("https://www.example.com/")) == {
        "https://www.example.com/sitemap-jobs.xml",
        "https://www.example.com/sitemap-main.xml",
    }
