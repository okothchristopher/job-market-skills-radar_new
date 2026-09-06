"""Store and model tests."""

from __future__ import annotations

from datetime import datetime

import pytest

from jobradar.store.db import JobStore
from jobradar.store.models import GROUP_GLOBAL, GROUP_KE, RawJob


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.sqlite")
    yield s
    s.close()


def make_job(**overrides) -> RawJob:
    defaults = dict(
        source="brightermonday",
        source_group=GROUP_KE,
        native_id="listing-1182963",
        url="https://www.brightermonday.co.ke/listings/qa-developer-5p64x6",
        title="Senior QA Automation Engineer",
        description_html="<p>Python, Selenium, CI/CD</p>",
        company="Med Bill, L.L.C",
        location="Remote (Work From Home)",
        date_posted=datetime(2026, 9, 6),
        fetched_at=datetime(2026, 9, 6),
    )
    defaults.update(overrides)
    return RawJob(**defaults)


# --------------------------------------------------------------- RawJob


def test_job_id_is_stable_across_instances():
    assert make_job().job_id == make_job().job_id


def test_job_id_differs_by_source():
    """The same posting syndicated to two boards keeps two identities here;
    collapsing them is the dedupe step's job, not identity's."""
    assert make_job().job_id != make_job(source="myjobmag").job_id


def test_year_and_month_derive_from_date_posted():
    job = make_job(date_posted=datetime(2025, 3, 14))
    assert job.year == 2025
    assert job.month == "2025-03"


def test_missing_date_yields_no_year():
    job = make_job(date_posted=None)
    assert job.year is None and job.month is None


def test_dedupe_key_normalises_title_and_company():
    a = make_job(title="Senior  QA Automation Engineer!", company="Med Bill, L.L.C")
    b = make_job(title="senior qa automation engineer", company="MedBill LLC")
    assert a.dedupe_key() == b.dedupe_key()


def test_dedupe_key_separates_different_roles():
    a = make_job(title="Backend Engineer")
    b = make_job(title="Frontend Engineer")
    assert a.dedupe_key() != b.dedupe_key()


# ---------------------------------------------------------------- JobStore


def test_upsert_and_count(store):
    store.upsert_job(make_job())
    assert store.job_count() == 1


def test_upsert_is_idempotent(store):
    """Re-collecting a posting updates it rather than duplicating it."""
    store.upsert_job(make_job())
    store.upsert_job(make_job(title="Senior QA Automation Engineer (Updated)"))
    assert store.job_count() == 1
    row = next(store.iter_jobs())
    assert row["title"].endswith("(Updated)")


def test_counts_by_source(store):
    store.upsert_jobs([make_job(), make_job(native_id="x2")])
    store.upsert_jobs([make_job(source="greenhouse", source_group=GROUP_GLOBAL, native_id="g1")])
    assert store.counts_by_source() == {"brightermonday": 2, "greenhouse": 1}


def test_counts_by_group_year_feeds_the_suppression_rule(store):
    store.upsert_jobs(
        [make_job(native_id=f"ke{i}", date_posted=datetime(2026, 1, 1)) for i in range(3)]
        + [
            make_job(
                source="greenhouse",
                source_group=GROUP_GLOBAL,
                native_id=f"g{i}",
                date_posted=datetime(2025, 6, 1),
            )
            for i in range(2)
        ]
    )
    assert store.counts_by_group_year() == [("GLOBAL", 2025, 2), ("KE", 2026, 3)]


def test_has_job_detects_prior_collection(store):
    store.upsert_job(make_job())
    assert store.has_job("brightermonday", "listing-1182963")
    assert not store.has_job("brightermonday", "never-seen")


def test_historical_flag_persists(store):
    """Wayback-replayed postings must stay distinguishable from live ones."""
    store.upsert_job(make_job(native_id="old", is_historical=True))
    row = next(store.iter_jobs())
    assert row["is_historical"] == 1


def test_date_posted_can_predate_fetched_at(store):
    """The Wayback case: a page archived in 2025 carrying a 2024 datePosted."""
    store.upsert_job(
        make_job(
            native_id="archived",
            date_posted=datetime(2024, 5, 2),
            fetched_at=datetime(2026, 9, 6),
            is_historical=True,
        )
    )
    row = next(store.iter_jobs())
    assert row["year"] == 2024
    assert row["fetched_at"].startswith("2026")


# ------------------------------------------------------------ crawl queue


def test_queue_is_resumable(store):
    urls = [f"https://example.com/job/{i}" for i in range(5)]
    store.enqueue(urls, source="brightermonday")
    assert store.queue_stats()["pending"] == 5

    for url in store.next_pending("brightermonday", limit=2):
        store.mark(url, "done")

    stats = store.queue_stats()
    assert stats["done"] == 2 and stats["pending"] == 3


def test_enqueue_ignores_duplicates(store):
    store.enqueue(["https://example.com/a"], source="s")
    store.enqueue(["https://example.com/a"], source="s")
    assert store.queue_stats()["pending"] == 1


def test_failures_are_recorded_not_swallowed(store):
    store.record_failure("https://example.com/a", "brightermonday", 503, "server error")
    assert store.failure_count() == 1
    assert store.failure_count("brightermonday") == 1
    assert store.failure_count("myjobmag") == 0


def test_store_survives_reopen(tmp_path):
    path = tmp_path / "jobs.sqlite"
    first = JobStore(path)
    first.upsert_job(make_job())
    first.close()

    second = JobStore(path)
    assert second.job_count() == 1
    second.close()


def test_counts_by_source_group_year_excludes_ats(store):
    """ATS boards carry real publish dates but a survivorship-biased tail, so
    they must not inflate the count of genuine history."""
    store.upsert_jobs(
        [
            make_job(source="hn_hiring", native_id=f"h{i}", date_posted=datetime(2024, 5, 1))
            for i in range(3)
        ]
        + [
            make_job(source="greenhouse", native_id=f"g{i}", date_posted=datetime(2024, 5, 1))
            for i in range(9)
        ]
    )
    assert store.counts_by_group_year() == [("KE", 2024, 12)]
    assert store.counts_by_source_group_year(["hn_hiring"]) == [("KE", 2024, 3)]


def test_counts_by_source_group_year_with_no_sources(store):
    store.upsert_job(make_job())
    assert store.counts_by_source_group_year([]) == []


# --------------------------------------------- regression: iteration deadlock


def test_can_write_while_iterating(store):
    """iter_jobs must not hold the store lock across a yield.

    Skill extraction reads every posting and writes its matches back as it
    goes. When the lock was held across the yield, the write blocked on a
    non-reentrant lock and the process hung silently -- no error, no progress,
    just a stalled run that looked like slow work.
    """
    from types import SimpleNamespace

    store.upsert_jobs([make_job(native_id=f"j{i}") for i in range(12)])

    written = 0
    for row in store.iter_jobs(batch_size=5):
        match = SimpleNamespace(
            skill="Python",
            canonical_name="python",
            category="language",
            zindua_track="Software Engineering Core",
            n_mentions=1,
            matched_in="description",
        )
        written += store.replace_job_skills(row["job_id"], [match])
    store.commit()

    assert written == 12
    assert store.skill_match_count() == 12
    assert store.jobs_with_skills_count() == 12


def test_iter_jobs_visits_every_row_once(store):
    store.upsert_jobs([make_job(native_id=f"k{i}") for i in range(23)])
    seen = [row["job_id"] for row in store.iter_jobs(batch_size=5)]
    assert len(seen) == 23
    assert len(set(seen)) == 23


def test_replace_job_skills_overwrites_previous_extraction(store):
    """Re-running extraction after a taxonomy fix must replace, not accumulate."""
    from types import SimpleNamespace

    def m(skill):
        return SimpleNamespace(
            skill=skill,
            canonical_name=skill.lower(),
            category="c",
            zindua_track="t",
            n_mentions=1,
            matched_in="description",
        )

    store.upsert_job(make_job())
    job_id = next(store.iter_jobs())["job_id"]
    store.replace_job_skills(job_id, [m("Excel"), m("Go")])
    store.commit()
    assert store.skill_match_count() == 2

    store.replace_job_skills(job_id, [m("Excel")])  # corrected taxonomy
    store.commit()
    assert store.skill_match_count() == 1


def test_history_counts_match_wayback_board_variants(store):
    """Replayed sources are stored as wayback_{board}, so 'wayback' must match by
    prefix. Exact matching made `status` report no Kenyan history while the
    analysis layer was already using thousands of backfilled postings."""
    store.upsert_jobs(
        [make_job(source="hn_hiring", native_id="h1", date_posted=datetime(2024, 1, 1))]
        + [
            make_job(
                source="wayback_brightermonday", native_id=f"w{i}", date_posted=datetime(2024, 6, 1)
            )
            for i in range(3)
        ]
        + [make_job(source="greenhouse", native_id="g1", date_posted=datetime(2024, 6, 1))]
    )
    counts = {(g, y): n for g, y, n in store.counts_by_source_group_year(["hn_hiring", "wayback"])}
    assert counts[("KE", 2024)] == 4, "wayback_* variants must be counted"


def test_history_counts_do_not_match_unrelated_prefixes(store):
    store.upsert_jobs(
        [make_job(source="waybackfill_other", native_id="x1", date_posted=datetime(2024, 1, 1))]
    )
    assert store.counts_by_source_group_year(["wayback"]) == []
