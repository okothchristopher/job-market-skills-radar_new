"""Global adapter tests.

Fixtures mirror the real payload shapes captured from each live API during
Phase 2, trimmed to the fields the adapters read.
"""

from __future__ import annotations

import json

from jobradar.fetch.client import Response
from jobradar.sources.ashby import AshbyAdapter
from jobradar.sources.greenhouse import GreenhouseAdapter
from jobradar.sources.hn_hiring import HNHiringAdapter
from jobradar.sources.remote_boards import (
    ArbeitnowAdapter,
    HimalayasAdapter,
    JobicyAdapter,
    RemotiveAdapter,
)
from jobradar.store.models import GROUP_GLOBAL


def response(url: str, payload) -> Response:
    return Response(
        url=url,
        status_code=200,
        headers={},
        body=json.dumps(payload).encode(),
        encoding="utf-8",
        from_cache=False,
    )


# ------------------------------------------------------------------ Greenhouse

GREENHOUSE_PAYLOAD = {
    "jobs": [
        {
            "id": 4567890,
            "title": "Senior Backend Engineer",
            "absolute_url": "https://boards.greenhouse.io/moniepoint/jobs/4567890",
            "location": {"name": "Lagos, Nigeria"},
            # Greenhouse double-encodes its HTML, hence the escaped tags.
            "content": "&lt;p&gt;We use &lt;strong&gt;Python&lt;/strong&gt; and K8s.&lt;/p&gt;",
            "first_published": "2026-07-08T13:32:53-04:00",
            "updated_at": "2026-09-04T14:12:20-04:00",
            "departments": [{"name": "Engineering"}],
            "offices": [{"name": "Lagos"}],
            "company_name": "Moniepoint",
        },
        {"id": None, "title": "Broken row with no id"},
    ]
}

GH_URL = "https://boards-api.greenhouse.io/v1/boards/moniepoint/jobs?content=true"


def greenhouse_adapter():
    return GreenhouseAdapter(
        client=None,
        config={},
        companies=[{"slug": "moniepoint", "ats": "greenhouse", "segment": "africa"}],
    )


def test_greenhouse_parses_a_posting():
    jobs = list(greenhouse_adapter().parse(response(GH_URL, GREENHOUSE_PAYLOAD)))
    assert len(jobs) == 1  # the id-less row is skipped, not crashed on
    job = jobs[0]
    assert job.title == "Senior Backend Engineer"
    assert job.company == "Moniepoint"
    assert job.location == "Lagos, Nigeria"
    assert job.department == "Engineering"
    assert job.source_group == GROUP_GLOBAL
    assert job.extra["segment"] == "africa"


def test_greenhouse_prefers_first_published_over_updated_at():
    """A board refresh bumps updated_at. Using it would smear every historical
    posting into the current month and destroy the trend axis."""
    job = next(iter(greenhouse_adapter().parse(response(GH_URL, GREENHOUSE_PAYLOAD))))
    assert job.month == "2026-07"  # first_published, not the September updated_at


def test_greenhouse_description_survives_double_encoding():
    job = next(iter(greenhouse_adapter().parse(response(GH_URL, GREENHOUSE_PAYLOAD))))
    text = GreenhouseAdapter.description_text(job)
    assert "Python" in text and "K8s" in text
    assert "&lt;" not in text


def test_greenhouse_discovers_only_greenhouse_boards():
    adapter = GreenhouseAdapter(
        client=None,
        config={},
        companies=[
            {"slug": "a", "ats": "greenhouse"},
            {"slug": "b", "ats": "ashby"},
        ],
    )
    urls = list(adapter.discover())
    assert len(urls) == 1 and "/boards/a/" in urls[0]


def test_greenhouse_handles_malformed_payload():
    assert list(greenhouse_adapter().parse(response(GH_URL, {"unexpected": True}))) == []


# ---------------------------------------------------------------------- Ashby

ASHBY_PAYLOAD = {
    "jobs": [
        {
            "id": "9829c4f1-548f-4ef9-b4a2-c0ba2e11b90c",
            "title": "Staff Fullstack Engineer",
            "location": "North America",
            "isRemote": True,
            "isListed": True,
            "publishedAt": "2026-03-04T20:00:29.167+00:00",
            "employmentType": "FullTime",
            "department": "Product & Engineering",
            "team": "Engineering",
            "jobUrl": "https://jobs.ashbyhq.com/andela/9829c4f1",
            "descriptionHtml": "<p>React and TypeScript</p>",
            "descriptionPlain": "React and TypeScript",
            "secondaryLocations": [],
        },
        {
            "id": "unlisted-1",
            "title": "Confidential Role",
            "isListed": False,
            "publishedAt": "2026-03-04T20:00:29.167+00:00",
        },
    ]
}

ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/andela?includeCompensation=true"


def ashby_adapter():
    return AshbyAdapter(
        client=None,
        config={},
        companies=[{"slug": "andela", "ats": "ashby", "segment": "africa"}],
    )


def test_ashby_parses_a_posting():
    jobs = list(ashby_adapter().parse(response(ASHBY_URL, ASHBY_PAYLOAD)))
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Staff Fullstack Engineer"
    assert job.is_remote is True
    assert job.employment_type == "FullTime"
    assert job.month == "2026-03"


def test_ashby_skips_unlisted_postings():
    """isListed=False means it exists but is not publicly advertised. Counting it
    would overstate demand."""
    jobs = list(ashby_adapter().parse(response(ASHBY_URL, ASHBY_PAYLOAD)))
    assert all(job.native_id != "unlisted-1" for job in jobs)


def test_ashby_slug_extraction_handles_query_string():
    assert AshbyAdapter.slug_from_url(ASHBY_URL) == "andela"


# ----------------------------------------------------------------------- HN

HN_PAYLOAD = {
    "id": 38842977,
    "title": "Ask HN: Who is hiring? (January 2024)",
    "created_at": "2024-01-02T16:00:00.000Z",
    "children": [
        {
            "id": 38843001,
            "author": "someone",
            "created_at": "2024-01-02T16:02:48.000Z",
            "text": (
                "Powertools Technologies | Junior/Senior Engineer | ONSITE | Full-time"
                "<p>We build embedded systems in Rust and C++. "
                "You will work on firmware, tooling and CI. Experience with "
                "Yocto or Buildroot is a plus. Send us a note if interested."
            ),
        },
        {"id": 38843002, "author": "b", "created_at": "2024-01-02T17:00:00.000Z", "text": "+1"},
        {"id": 38843003, "author": "c", "created_at": "2024-01-02T18:00:00.000Z", "text": None},
    ],
}

HN_URL = "https://hn.algolia.com/api/v1/items/38842977"


def hn_adapter():
    return HNHiringAdapter(client=None, config={"since": "2024-01"})


def test_hn_parses_job_posts_and_skips_chatter():
    jobs = list(hn_adapter().parse(response(HN_URL, HN_PAYLOAD)))
    assert len(jobs) == 1  # "+1" is too short; the None-text comment is deleted
    job = jobs[0]
    assert job.company == "Powertools Technologies"
    assert job.title == "Junior/Senior Engineer"
    assert job.employment_type == "Full-time"
    assert job.is_remote is False


def test_hn_dates_each_post_to_its_own_thread_month():
    """This is why HN is the historical spine: every posting carries the exact
    month it was written, so the monthly series never smears."""
    job = next(iter(hn_adapter().parse(response(HN_URL, HN_PAYLOAD))))
    assert job.month == "2024-01"
    assert job.extra["thread_month"] == "2024-01"


def test_hn_ignores_the_sibling_wants_to_be_hired_thread():
    """Those are candidates advertising themselves, not employers hiring.
    Counting them would invert the signal."""
    payload = dict(HN_PAYLOAD, title="Ask HN: Who wants to be hired? (January 2024)")
    assert list(hn_adapter().parse(response(HN_URL, payload))) == []


def test_hn_header_parsing_returns_none_rather_than_guessing():
    """A wrongly-parsed company name corrupts the cross-board dedupe key."""
    company, location, employment = HNHiringAdapter._parse_header("No pipes in this line at all")
    assert (company, location, employment) == (None, None, None)


# -------------------------------------------------------------- remote boards


def test_remotive_parses():
    payload = {
        "jobs": [
            {
                "id": 1234,
                "url": "https://remotive.com/remote-jobs/x",
                "title": "Data Engineer",
                "company_name": "Acme",
                "category": "Data",
                "job_type": "full_time",
                "publication_date": "2026-08-01T10:00:00",
                "candidate_required_location": "Worldwide",
                "description": "<p>dbt, Airflow, Snowflake</p>",
                "tags": ["dbt", "airflow"],
            }
        ]
    }
    job = next(
        iter(
            RemotiveAdapter(None, {}).parse(
                response("https://remotive.com/api/remote-jobs", payload)
            )
        )
    )
    assert job.title == "Data Engineer"
    assert job.is_remote is True
    assert job.month == "2026-08"


def test_arbeitnow_parses():
    payload = {
        "data": [
            {
                "slug": "backend-dev-berlin-123",
                "title": "Backend Developer",
                "company_name": "Berlin GmbH",
                "description": "<p>Go and Postgres</p>",
                "remote": False,
                "location": "Berlin",
                "job_types": ["full_time"],
                "created_at": 1754035200,
                "url": "https://arbeitnow.com/view/x",
                "tags": [],
            }
        ]
    }
    job = next(
        iter(
            ArbeitnowAdapter(None, {}).parse(
                response("https://www.arbeitnow.com/api/job-board-api", payload)
            )
        )
    )
    assert job.title == "Backend Developer"
    assert job.is_remote is False
    assert job.date_posted is not None  # epoch seconds parsed


def test_jobicy_parses_salary_fields():
    payload = {
        "jobs": [
            {
                "id": 99,
                "url": "https://jobicy.com/jobs/x",
                "jobTitle": "ML Engineer",
                "companyName": "Acme",
                "jobDescription": "<p>PyTorch</p>",
                "jobGeo": "Anywhere",
                "jobType": ["full-time"],
                "jobIndustry": ["Data Science"],
                "pubDate": "2026-08-15 10:00:00",
                "salaryMin": "90000",
                "salaryMax": "",
                "salaryCurrency": "USD",
            }
        ]
    }
    job = next(
        iter(
            JobicyAdapter(None, {}).parse(
                response("https://jobicy.com/api/v2/remote-jobs", payload)
            )
        )
    )
    assert job.salary_min == 90000.0
    assert job.salary_max is None  # empty string must not become 0.0
    assert job.salary_currency == "USD"


def test_himalayas_parses():
    payload = {
        "jobs": [
            {
                "guid": "abc-123",
                "title": "Platform Engineer",
                "companyName": "Acme",
                "description": "<p>Terraform, AWS</p>",
                "employmentType": "Full Time",
                "locationRestrictions": ["United States", "Canada"],
                "pubDate": 1754035200,
                "expiryDate": 1756713600,
                "minSalary": 120000,
                "maxSalary": 160000,
                "currency": "USD",
                "seniority": ["Senior"],
                "applicationLink": "https://himalayas.app/jobs/x",
            }
        ]
    }
    job = next(
        iter(HimalayasAdapter(None, {}).parse(response("https://himalayas.app/jobs/api", payload)))
    )
    assert job.location == "United States, Canada"
    assert job.salary_min == 120000.0 and job.salary_max == 160000.0


def test_remote_board_attribution_is_exposed():
    """Remotive and Jobicy both require credit in any published report."""
    adapter = RemotiveAdapter(None, {"attribution": "Job data via Remotive (https://remotive.com)"})
    assert "Remotive" in adapter.attribution()


def test_remote_boards_survive_empty_payloads():
    for cls, url in [
        (RemotiveAdapter, "https://remotive.com/api/remote-jobs"),
        (ArbeitnowAdapter, "https://www.arbeitnow.com/api/job-board-api"),
        (JobicyAdapter, "https://jobicy.com/api/v2/remote-jobs"),
        (HimalayasAdapter, "https://himalayas.app/jobs/api"),
    ]:
        assert list(cls(None, {}).parse(response(url, {}))) == []
