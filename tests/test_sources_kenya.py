"""Kenyan adapter tests.

The crawl-shape assertions matter as much as the parsing ones: BrighterMonday and
MyJobMag both disallow the URL patterns a naive keyword-search crawler would
generate, so a regression here means either a robots violation or a silently
empty source.
"""

from __future__ import annotations

import pytest
from fixtures_html import BRIGHTERMONDAY_HTML, MYJOBMAG_HTML

from jobradar.fetch.client import Response
from jobradar.sources.kenya import (
    BrighterMondayAdapter,
    FuzuAdapter,
    JobWebKenyaAdapter,
    MyJobMagAdapter,
)
from jobradar.store.models import GROUP_KE, GROUP_REGION


def page(url: str, html: str) -> Response:
    return Response(
        url=url,
        status_code=200,
        headers={},
        body=html.encode(),
        encoding="utf-8",
        from_cache=False,
    )


# ------------------------------------------------------------ BrighterMonday


def bm(config=None):
    return BrighterMondayAdapter(client=None, config=config or {"max_list_page": 7})


BM_LISTING = """
<a href="https://www.brightermonday.co.ke/listings/qa-developer-5p64x6">QA</a>
<a href="https://www.brightermonday.co.ke/listings/data-analyst-abc123">Data</a>
<a href="https://www.brightermonday.co.ke/listings/qa-developer-5p64x6">dup</a>
<a href="/about-us">not a job</a>
"""


def test_brightermonday_extracts_listing_links():
    links = bm().detail_links(
        page("https://www.brightermonday.co.ke/jobs/software-data", BM_LISTING)
    )
    assert len(links) == 2  # duplicate collapsed, non-job ignored
    assert all("/listings/" in u for u in links)


def test_brightermonday_parses_a_posting():
    job = bm().parse_detail(
        page("https://www.brightermonday.co.ke/listings/qa-developer-5p64x6", BRIGHTERMONDAY_HTML)
    )
    assert job.title == "Senior QA Automation Engineer"
    assert job.company == "Med Bill, L.L.C"  # resolved through the @id reference
    assert job.is_remote is True
    assert job.location == "KE"  # from applicantLocationRequirements
    assert job.month == "2026-07"
    assert (job.salary_min, job.salary_max, job.salary_currency) == (250000.0, 300000.0, "KES")
    assert job.source_group == GROUP_KE
    assert job.country == "KE"


def test_detail_page_yields_no_further_links():
    """Following links out of a detail page wanders into 'similar jobs' and
    silently multiplies the crawl."""
    detail = page("https://www.brightermonday.co.ke/listings/qa-developer-5p64x6", BM_LISTING)
    assert list(bm().next_urls(detail)) == []


def test_listing_page_yields_details_and_pages():
    listing = page("https://www.brightermonday.co.ke/jobs/software-data", BM_LISTING)
    urls = list(bm().next_urls(listing))
    assert any("/listings/" in u for u in urls)
    assert any("page=" in u for u in urls)


def test_listing_page_parses_no_jobs():
    listing = page("https://www.brightermonday.co.ke/jobs/software-data", BM_LISTING)
    assert list(bm().parse(listing)) == []


# ------------------------------------------------------------------ MyJobMag

MJM_LISTING = """
<a href="/job/account-manager-tugende-21">A</a>
<a href="/job/application-security-engineer-talent-safari">B</a>
<a href="/jobs">index, not a job</a>
"""


def mjm(config=None):
    return MyJobMagAdapter(client=None, config=config or {"max_pages": 4})


def test_myjobmag_extracts_and_absolutises_links():
    links = mjm().detail_links(
        page("https://www.myjobmag.co.ke/jobs-by-field/information-technology", MJM_LISTING)
    )
    assert len(links) == 2
    assert all(u.startswith("https://www.myjobmag.co.ke/job/") for u in links)


def test_myjobmag_uses_path_pagination_not_query_strings():
    """robots.txt disallows every query string (/*?), so ?page=2 would be a
    violation. Path pagination is what it permits."""
    urls = mjm().pagination_urls(
        page("https://www.myjobmag.co.ke/jobs-by-field/information-technology", "")
    )
    assert urls == ["https://www.myjobmag.co.ke/jobs-by-field/information-technology/2"]
    assert not any("?" in u for u in urls)


def test_myjobmag_pagination_advances_and_stops_at_the_cap():
    adapter = mjm({"max_pages": 4})
    base = "https://www.myjobmag.co.ke/jobs-by-field/information-technology"
    assert adapter.pagination_urls(page(f"{base}/2", "")) == [f"{base}/3"]
    assert adapter.pagination_urls(page(f"{base}/4", "")) == []


def test_myjobmag_parses_despite_control_characters():
    job = mjm().parse_detail(
        page("https://www.myjobmag.co.ke/job/account-manager-tugende-21", MYJOBMAG_HTML)
    )
    assert job is not None, "raw CR/LF in the JSON-LD must not lose the posting"
    assert job.title == "Account Manager"
    assert job.company == "Tugende"
    assert job.location == "Mombasa, KE"
    assert job.month == "2026-09"


# ---------------------------------------------------------------------- Fuzu

FUZU_CATEGORY = """
<script type="application/ld+json">
{"@context":"http://schema.org","@type":"ItemList","itemListElement":[
 {"@type":"ListItem","position":1,"name":"Senior Engineering Manager",
  "url":"https://www.fuzu.com/kenya/jobs/senior-engineering-manager-givedirectly"},
 {"@type":"ListItem","position":2,"name":"Data Analyst",
  "url":"https://www.fuzu.com/kenya/jobs/data-analyst-acme-1234abcd"}]}
</script>
<a href="/kenya/jobs/case-manager-oasis-outsourcing-aaa7c656">C</a>
"""

FUZU_LANDING = """
<a href="/kenya/job/computers-software-development">Software</a>
<a href="/kenya/job/data-research">Data</a>
<a href="/kenya/job/senior">Senior</a>
<a href="/kenya/job/nairobi">Nairobi</a>
"""


def fuzu(config=None):
    return FuzuAdapter(client=None, config=config or {"countries": ["kenya"], "max_pages": 3})


def test_fuzu_reads_job_urls_from_the_itemlist():
    """The ItemList is the site's own declaration of what is on the page, so it
    survives markup changes that would break anchor scraping."""
    links = fuzu().detail_links(
        page("https://www.fuzu.com/kenya/job/computers-software-development", FUZU_CATEGORY)
    )
    assert len(links) == 3  # two from ItemList, one from the anchor
    assert all("/jobs/" in u for u in links)


def test_fuzu_landing_page_fans_out_to_real_categories_only():
    """Seniority and location filters share the category URL shape. Crawling
    them just re-walks the same postings under a different name."""
    urls = fuzu().pagination_urls(page("https://www.fuzu.com/kenya/job", FUZU_LANDING))
    slugs = {u.rsplit("/", 1)[-1] for u in urls}
    assert slugs == {"computers-software-development", "data-research"}


def test_fuzu_distinguishes_category_from_job_urls():
    """/kenya/job/x is a category; /kenya/jobs/x is a posting. Conflating them
    yields a crawl that finds no postings at all."""
    adapter = fuzu()
    assert adapter.is_detail_url("https://www.fuzu.com/kenya/jobs/data-analyst-acme-1234")
    assert not adapter.is_detail_url("https://www.fuzu.com/kenya/job/data-research")


def test_fuzu_tags_kenya_and_region_separately():
    """D2 collects Uganda and Nigeria too, but Kenya is the analysis target and
    must stay separable."""
    adapter = fuzu()
    ke = adapter.parse_detail(
        page("https://www.fuzu.com/kenya/jobs/qa-abc123", BRIGHTERMONDAY_HTML)
    )
    ug = adapter.parse_detail(
        page("https://www.fuzu.com/uganda/jobs/qa-abc123", BRIGHTERMONDAY_HTML)
    )
    assert (ke.source_group, ke.country) == (GROUP_KE, "KE")
    assert (ug.source_group, ug.country) == (GROUP_REGION, "UG")


def test_fuzu_discovers_one_landing_page_per_country():
    urls = list(FuzuAdapter(None, {"countries": ["kenya", "uganda"]}).discover())
    assert urls == ["https://www.fuzu.com/kenya/job", "https://www.fuzu.com/uganda/job"]


# --------------------------------------------------------------- JobWebKenya


def test_jobwebkenya_pagination_is_capped():
    """This source declares Crawl-delay: 60, so every extra page costs a minute
    of wall-clock. The budget is deliberately small."""
    adapter = JobWebKenyaAdapter(client=None, config={"max_pages": 2})
    base = "https://jobwebkenya.com/jobs"
    assert adapter.pagination_urls(page(base, "")) == [f"{base}/page/2/"]
    assert adapter.pagination_urls(page(f"{base}/page/2/", "")) == []


def test_jobwebkenya_detail_pattern():
    adapter = JobWebKenyaAdapter(client=None, config={})
    assert adapter.is_detail_url("https://jobwebkenya.com/jobs/dentist-avenue-healthcare/")
    assert not adapter.is_detail_url("https://jobwebkenya.com/job-location/nairobi/")


# ------------------------------------------------------------------- general


@pytest.mark.parametrize(
    "adapter_cls", [BrighterMondayAdapter, MyJobMagAdapter, FuzuAdapter, JobWebKenyaAdapter]
)
def test_pages_without_jsonld_are_skipped_not_crashed(adapter_cls):
    adapter = adapter_cls(client=None, config={})
    assert adapter.parse_detail(page("https://example.com/job/x", "<html>nope</html>")) is None


@pytest.mark.parametrize(
    "adapter_cls", [BrighterMondayAdapter, MyJobMagAdapter, FuzuAdapter, JobWebKenyaAdapter]
)
def test_no_adapter_generates_a_keyword_search_url(adapter_cls):
    """The previous project's whole approach was keyword-search URLs. Both
    BrighterMonday and MyJobMag disallow those outright."""
    adapter = adapter_cls(client=None, config={"categories": ["/jobs/software-data"]})
    generated = list(adapter.discover()) + adapter.pagination_urls(
        page("https://example.com/jobs/software-data", "")
    )
    for url in generated:
        assert "q=" not in url and "keywords=" not in url and "search" not in url


# ------------------------------------------- regression: speculative pagination


def test_pagination_stops_when_a_page_has_no_postings():
    """BrighterMonday's software-data category holds three pages. Fanning out
    blindly to the robots ceiling of 7 requested four pages that do not exist,
    404-ing each and filling the failure table with our own mistakes instead of
    real problems."""
    empty = page("https://www.brightermonday.co.ke/jobs/software-data?page=3", "<html></html>")
    assert list(bm().next_urls(empty)) == []


def test_pagination_advances_one_page_at_a_time():
    adapter = bm()
    first = page("https://www.brightermonday.co.ke/jobs/software-data", BM_LISTING)
    assert adapter.pagination_urls(first) == [
        "https://www.brightermonday.co.ke/jobs/software-data?page=2"
    ]
    second = page("https://www.brightermonday.co.ke/jobs/software-data?page=2", BM_LISTING)
    assert adapter.pagination_urls(second) == [
        "https://www.brightermonday.co.ke/jobs/software-data?page=3"
    ]


def test_pagination_never_exceeds_the_robots_ceiling():
    last = page("https://www.brightermonday.co.ke/jobs/software-data?page=7", BM_LISTING)
    assert bm().pagination_urls(last) == []


def test_fuzu_landing_page_fans_out_despite_having_no_postings():
    """The landing page lists categories, not jobs, so an empty detail-link
    result there is expected rather than the end of the crawl."""
    landing = page("https://www.fuzu.com/kenya/job", FUZU_LANDING)
    urls = list(fuzu().next_urls(landing))
    assert len(urls) == 2
