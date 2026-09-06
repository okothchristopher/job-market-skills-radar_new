"""Wayback replay adapter tests.

This adapter is the only source of Kenyan history, so its failure modes are
expensive: a silent miss here leaves the diffusion engine permanently unable to
separate ``teach_ahead`` from ``global_only``.
"""

from __future__ import annotations

import json

import pytest
from fixtures_html import BRIGHTERMONDAY_HTML

from jobradar.fetch.client import Response
from jobradar.sources.wayback import REPLAY_URL, WaybackAdapter


def response(url: str, body: str) -> Response:
    return Response(
        url=url,
        status_code=200,
        headers={},
        body=body.encode(),
        encoding="utf-8",
        from_cache=False,
    )


class FakeClient:
    """Returns a canned CDX payload and records the URLs requested."""

    def __init__(self, cdx_rows):
        self.cdx_rows = cdx_rows
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return response(url, json.dumps(self.cdx_rows))


def adapter(cdx_rows=None, **config):
    client = FakeClient(cdx_rows or [["original", "timestamp"]])
    base = {"replays": ["brightermonday"], "years": [2025], "max_urls_per_board": 100}
    return WaybackAdapter(client, {**base, **config})


# ----------------------------------------------------------------- discovery


def test_cdx_query_filters_server_side():
    """CDX prefix-matches and sorts alphabetically, so "myjobmag.co.ke/job/*"
    returns thousands of "/job-application/" apply forms first -- they sort
    before "/job/" -- and any row limit truncates before a real posting appears.
    Filtering server-side is the difference between 520 postings and zero.
    """
    a = adapter(replays=["myjobmag"])
    list(a.discover())
    query = a.client.calls[0]
    assert "filter=original:" in query
    assert "myjobmag" in query and "/job/" in query
    assert "collapse=urlkey" in query
    assert "filter=statuscode:200" in query


def test_discovery_builds_raw_replay_urls():
    """The id_ suffix returns the originally-archived bytes rather than the page
    wrapped in the archive's navigation toolbar."""
    rows = [
        ["original", "timestamp"],
        ["https://www.brightermonday.co.ke/listings/qa-developer-5p64x6", "20250519105520"],
    ]
    urls = list(adapter(rows).discover())
    assert urls == [
        "http://web.archive.org/web/20250519105520id_/"
        "https://www.brightermonday.co.ke/listings/qa-developer-5p64x6"
    ]


def test_non_posting_urls_are_skipped_before_spending_a_request():
    rows = [
        ["original", "timestamp"],
        ["https://www.brightermonday.co.ke/listings/real-job-abc123", "20250101000000"],
        ["https://www.brightermonday.co.ke/about-us", "20250101000000"],
        ["https://www.brightermonday.co.ke/blog/hiring-tips", "20250101000000"],
    ]
    urls = list(adapter(rows).discover())
    assert len(urls) == 1 and "real-job-abc123" in urls[0]


def test_tracking_parameter_variants_collapse_to_one():
    """collapse=urlkey does not collapse tracking parameters, so one posting
    appears once per campaign it was shared under. Left alone these burn the
    replay budget re-fetching pages we already have."""
    rows = [
        ["original", "timestamp"],
        ["https://www.brightermonday.co.ke/listings/caregivers-prn-wpdgm4", "20250101000000"],
        [
            "https://www.brightermonday.co.ke/listings/caregivers-prn-wpdgm4?utm_source=KenyaMOJA.com",
            "20250102000000",
        ],
        [
            "https://www.brightermonday.co.ke/listings/caregivers-prn-wpdgm4/",
            "20250103000000",
        ],
    ]
    urls = list(adapter(rows).discover())
    assert len(urls) == 1


def test_budget_caps_the_replay_queue():
    """~19,800 BrighterMonday listings were archived in 2025; replaying all of
    them takes hours, so the crawl accumulates across runs instead."""
    rows = [["original", "timestamp"]] + [
        [f"https://www.brightermonday.co.ke/listings/job-{i}-abc", "20250101000000"]
        for i in range(50)
    ]
    assert len(list(adapter(rows, max_urls_per_board=10).discover())) == 10


def test_cdx_failure_yields_nothing_rather_than_raising():
    class Broken:
        def get(self, url, **kwargs):
            return response(url, "not json at all")

    a = WaybackAdapter(Broken(), {"replays": ["brightermonday"], "years": [2025]})
    assert list(a.discover()) == []


# ------------------------------------------------------------------- parsing


REPLAY = REPLAY_URL.format(
    timestamp="20250519105520",
    url="https://www.brightermonday.co.ke/listings/qa-developer-5p64x6",
)


def test_archived_page_parses_through_the_live_parser():
    """Archived bytes still carry the site's JSON-LD, so the same parser reads
    them -- no separate archive-shaped parser to drift out of sync."""
    jobs = list(adapter().parse(response(REPLAY, BRIGHTERMONDAY_HTML)))
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Senior QA Automation Engineer"
    assert job.company == "Med Bill, L.L.C"
    assert job.is_historical is True
    assert job.source == "wayback_brightermonday"


def test_native_id_matches_what_the_live_crawl_would_produce():
    """This is what makes a replayed posting recognisable as the same posting,
    so it can be de-duplicated rather than double-counted."""
    job = next(iter(adapter().parse(response(REPLAY, BRIGHTERMONDAY_HTML))))
    assert job.native_id == "qa-developer-5p64x6"


def test_posting_date_is_read_from_the_page_not_the_archive_date():
    """Archive date and posting date are different things. A page crawled in May
    2025 can carry a datePosted from 2024, and that is precisely how 2024 data
    surfaces at all -- direct 2024 crawls of these URLs number in single digits.
    """
    job = next(iter(adapter().parse(response(REPLAY, BRIGHTERMONDAY_HTML))))
    assert job.month == "2026-07"  # from the page's datePosted
    assert job.extra["archive_timestamp"] == "20250519105520"


def test_provenance_is_recorded():
    job = next(iter(adapter().parse(response(REPLAY, BRIGHTERMONDAY_HTML))))
    assert job.extra["replayed_board"] == "brightermonday"
    assert job.extra["archive_url"] == REPLAY


def test_board_is_identified_from_the_original_url():
    a = adapter()
    assert a.board_for("https://www.brightermonday.co.ke/listings/x") == "brightermonday"
    assert a.board_for("https://www.myjobmag.co.ke/job/x") == "myjobmag"
    assert a.board_for("https://example.com/jobs/x") is None


def test_original_url_is_recovered_from_the_replay_url():
    assert (
        WaybackAdapter.original_url(REPLAY)
        == "https://www.brightermonday.co.ke/listings/qa-developer-5p64x6"
    )
    assert WaybackAdapter.original_url("https://not-an-archive-url.example/x") is None


@pytest.mark.parametrize("body", ["<html>no structured data</html>", "", "<script>broken</script>"])
def test_unparseable_archive_pages_are_skipped(body):
    assert list(adapter().parse(response(REPLAY, body))) == []


def test_unknown_board_is_ignored():
    url = REPLAY_URL.format(timestamp="20250101000000", url="https://example.com/jobs/x")
    assert list(adapter().parse(response(url, BRIGHTERMONDAY_HTML))) == []
