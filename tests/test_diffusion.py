"""Diffusion engine tests.

The engine's job is to stop plausible-looking nonsense reaching a curriculum
committee, so most of these assert that it *refuses* to conclude things.
"""

from __future__ import annotations

import pandas as pd
import pytest

from jobradar.aggregate import diffusion as dif
from jobradar.aggregate import frames as fr


@pytest.fixture
def settings():
    return dif.DiffusionSettings(
        calibration_skills=["SQL", "Git", "Java", "Excel"],
        teach_ahead_min_global_share=0.05,
        teach_ahead_max_kenya_share=0.02,
        teach_ahead_min_global_trend=0.01,
        min_cell_size=100,
        min_months_for_lag=12,
    )


def current(rows):
    """rows: (skill, track, group, share)"""
    return pd.DataFrame(
        [
            {
                "skill": s,
                "zindua_track": t,
                "category": "c",
                "source_group": g,
                "pct_share": v,
                "usable": True,
            }
            for s, t, g, v in rows
        ]
    )


def months(rows):
    """rows: (skill, month, share) for GLOBAL"""
    return pd.DataFrame(
        [{"skill": s, "source_group": "GLOBAL", "month": m, "pct_share": v} for s, m, v in rows]
    )


# ------------------------------------------------------- denominator choice


def test_pct_of_tech_is_the_comparable_denominator():
    """The corpora are not comparable on '% of all postings'.

    Measured on the real data: 80% of global postings mention a technical skill
    against 27% of Kenyan ones, because global sources are ATS boards of tech
    companies while Kenyan boards carry every sector. Dividing by all postings
    dilutes Kenyan tech skills ~3x and manufactures a gap from composition alone.
    """
    jobs = pd.DataFrame(
        # Global: 2 postings, both technical.
        [
            {"job_id": f"g{i}", "source_group": "GLOBAL", "year": 2026, "month": "2026-01"}
            for i in range(2)
        ]
        # Kenya: 2 technical postings plus 6 non-tech ones.
        + [
            {"job_id": f"k{i}", "source_group": "KE", "year": 2026, "month": "2026-01"}
            for i in range(8)
        ]
    )
    job_skills = pd.DataFrame(
        [
            {
                "job_id": "g0",
                "skill": "Python",
                "zindua_track": "t",
                "category": "c",
                "source_group": "GLOBAL",
                "year": 2026,
                "month": "2026-01",
            },
            {
                "job_id": "g1",
                "skill": "Python",
                "zindua_track": "t",
                "category": "c",
                "source_group": "GLOBAL",
                "year": 2026,
                "month": "2026-01",
            },
            {
                "job_id": "k0",
                "skill": "Python",
                "zindua_track": "t",
                "category": "c",
                "source_group": "KE",
                "year": 2026,
                "month": "2026-01",
            },
            {
                "job_id": "k1",
                "skill": "Python",
                "zindua_track": "t",
                "category": "c",
                "source_group": "KE",
                "year": 2026,
                "month": "2026-01",
            },
        ]
    )
    frame = fr.build_skill_share(jobs, job_skills, ["source_group"], min_cell_size=1)
    ke = frame[frame.source_group == "KE"].iloc[0]
    gl = frame[frame.source_group == "GLOBAL"].iloc[0]

    # Raw share says Kenya wants Python far less. It does not -- 6 of its 8
    # postings are simply not technical roles.
    assert ke["pct_of_all"] == pytest.approx(0.25)
    assert gl["pct_of_all"] == pytest.approx(1.0)

    # On the comparable denominator both markets ask for it equally.
    assert ke["pct_of_tech"] == pytest.approx(1.0)
    assert gl["pct_of_tech"] == pytest.approx(1.0)
    assert frame["pct_share"].equals(frame["pct_of_tech"])


def test_thinness_is_judged_on_technical_postings():
    """5,000 postings with 40 technical ones cannot support a claim about tech
    skills, however large the raw count looks."""
    jobs = pd.DataFrame(
        [
            {"job_id": f"k{i}", "source_group": "KE", "year": 2026, "month": "2026-01"}
            for i in range(500)
        ]
    )
    job_skills = pd.DataFrame(
        [
            {
                "job_id": f"k{i}",
                "skill": "Python",
                "zindua_track": "t",
                "category": "c",
                "source_group": "KE",
                "year": 2026,
                "month": "2026-01",
            }
            for i in range(40)
        ]
    )
    frame = fr.build_skill_share(jobs, job_skills, ["source_group"], min_cell_size=100)
    assert frame.iloc[0]["n_jobs_total"] == 500
    assert not frame.iloc[0]["usable"]


# ------------------------------------------------------------- calibration


def test_calibration_offset_is_the_baseline_median(settings):
    """Every global source skews ahead of Kenya, so nearly every skill shows a
    positive gap. Subtracting the fully-diffused skills' median is what
    separates a real signal from 'this technology is modern'."""
    frame = current(
        [
            ("SQL", "t", "GLOBAL", 0.30),
            ("SQL", "t", "KE", 0.20),
            ("Git", "t", "GLOBAL", 0.25),
            ("Git", "t", "KE", 0.15),
            ("Java", "t", "GLOBAL", 0.20),
            ("Java", "t", "KE", 0.10),
            ("Rust", "t", "GLOBAL", 0.15),
            ("Rust", "t", "KE", 0.01),
        ]
    )
    out, diag = dif.build_diffusion(frame, months([]), settings)
    assert diag["calibration_applied"] is True
    assert diag["calibration_offset"] == pytest.approx(0.10)

    rust = out[out.skill == "Rust"].iloc[0]
    assert rust["diffusion_gap"] == pytest.approx(0.14)
    assert rust["calibrated_gap"] == pytest.approx(0.04)  # 0.14 - 0.10


def test_calibration_is_skipped_when_too_few_baseline_skills(settings):
    """Correcting by a number built on two rows is worse than not correcting."""
    frame = current([("SQL", "t", "GLOBAL", 0.3), ("SQL", "t", "KE", 0.2)])
    _, diag = dif.build_diffusion(frame, months([]), settings)
    assert diag["calibration_applied"] is False
    assert diag["calibration_offset"] == 0.0


# --------------------------------------------------------------- statuses


def test_teach_ahead_requires_large_absent_and_rising(settings):
    frame = current(
        [
            ("SQL", "t", "GLOBAL", 0.2),
            ("SQL", "t", "KE", 0.2),
            ("Git", "t", "GLOBAL", 0.2),
            ("Git", "t", "KE", 0.2),
            ("Java", "t", "GLOBAL", 0.2),
            ("Java", "t", "KE", 0.2),
            ("AI Agents", "AI Engineering", "GLOBAL", 0.20),
            ("AI Agents", "AI Engineering", "KE", 0.005),
        ]
    )
    series = months(
        [("AI Agents", f"2025-{m:02d}", 0.05) for m in range(1, 7)]
        + [("AI Agents", f"2026-{m:02d}", 0.20) for m in range(1, 7)]
    )
    out, _ = dif.build_diffusion(frame, series, settings)
    row = out[out.skill == "AI Agents"].iloc[0]
    assert row["status"] == dif.TEACH_AHEAD
    assert row["global_trend_12m"] > 0


def test_large_but_flat_globally_and_absent_locally_is_global_only(settings):
    """It has had time to arrive and has not. Teaching it on global signal alone
    is the mistake the status exists to prevent."""
    frame = current(
        [
            ("SQL", "t", "GLOBAL", 0.2),
            ("SQL", "t", "KE", 0.2),
            ("Git", "t", "GLOBAL", 0.2),
            ("Git", "t", "KE", 0.2),
            ("Java", "t", "GLOBAL", 0.2),
            ("Java", "t", "KE", 0.2),
            ("Kubernetes", "DevOps Engineering", "GLOBAL", 0.13),
            ("Kubernetes", "DevOps Engineering", "KE", 0.005),
        ]
    )
    series = months([("Kubernetes", f"2025-{m:02d}", 0.13) for m in range(1, 13)])
    out, _ = dif.build_diffusion(frame, series, settings)
    assert out[out.skill == "Kubernetes"].iloc[0]["status"] == dif.GLOBAL_ONLY


def test_kenya_specific_is_detected(settings):
    """Real local demand the global signal is structurally blind to."""
    frame = current(
        [
            ("SQL", "t", "GLOBAL", 0.2),
            ("SQL", "t", "KE", 0.2),
            ("Git", "t", "GLOBAL", 0.2),
            ("Git", "t", "KE", 0.2),
            ("Java", "t", "GLOBAL", 0.2),
            ("Java", "t", "KE", 0.2),
            ("M-Pesa", "Software Engineering Core", "GLOBAL", 0.001),
            ("M-Pesa", "Software Engineering Core", "KE", 0.15),
        ]
    )
    out, _ = dif.build_diffusion(frame, months([]), settings)
    assert out[out.skill == "M-Pesa"].iloc[0]["status"] == dif.KENYA_SPECIFIC


def test_small_everywhere_is_low_demand_not_insufficient_data(settings):
    """We have ample data on these. Labelling them 'insufficient' would hide a
    real answer behind a data-quality excuse."""
    frame = current(
        [
            ("SQL", "t", "GLOBAL", 0.2),
            ("SQL", "t", "KE", 0.2),
            ("Git", "t", "GLOBAL", 0.2),
            ("Git", "t", "KE", 0.2),
            ("Java", "t", "GLOBAL", 0.2),
            ("Java", "t", "KE", 0.2),
            ("Perl", "Software Engineering Core", "GLOBAL", 0.004),
            ("Perl", "Software Engineering Core", "KE", 0.001),
        ]
    )
    out, _ = dif.build_diffusion(frame, months([]), settings)
    assert out[out.skill == "Perl"].iloc[0]["status"] == dif.LOW_DEMAND


def test_thin_cells_are_marked_insufficient(settings):
    """insufficient_data means the sample cannot support a judgement -- not that
    the skill is unpopular."""
    frame = current([("Rust", "t", "GLOBAL", 0.2), ("Rust", "t", "KE", 0.0)])
    frame["usable"] = False
    out, _ = dif.build_diffusion(frame, months([]), settings)
    assert out.iloc[0]["status"] == dif.INSUFFICIENT
    assert out.iloc[0]["confidence"] == "none"


def test_absence_scores_zero_rather_than_unknown(settings):
    """A skill missing from a segment was searched for and not found. Treating
    that as unknown would drop it from the comparison entirely."""
    frame = current([("Rust", "t", "GLOBAL", 0.2)])  # no KE row at all
    out, _ = dif.build_diffusion(frame, months([]), settings)
    assert out.iloc[0]["kenya_share_now"] == 0.0


# ------------------------------------------------ what must NOT be computed


def test_lag_and_kenya_trend_stay_null_without_kenyan_history(settings):
    """Live boards delete expired postings, so the Kenyan corpus is single-year.
    Inventing a lag from it is the easiest way to be confidently wrong."""
    frame = current([("Python", "t", "GLOBAL", 0.3), ("Python", "t", "KE", 0.1)])
    out, diag = dif.build_diffusion(frame, months([]), settings, kenya_months_observed=5)

    assert out["estimated_lag_months"].isna().all()
    assert out["kenya_trend_12m"].isna().all()
    assert diag["kenya_history_sufficient"] is False
    assert diag["lag_estimate_enabled"] is False


def test_teach_ahead_is_provisional_without_kenyan_history(settings):
    """teach_ahead and global_only cannot be fully separated without a Kenyan
    series; the global trend is a proxy, so confidence must say so."""
    frame = current(
        [
            ("SQL", "t", "GLOBAL", 0.2),
            ("SQL", "t", "KE", 0.2),
            ("Git", "t", "GLOBAL", 0.2),
            ("Git", "t", "KE", 0.2),
            ("Java", "t", "GLOBAL", 0.2),
            ("Java", "t", "KE", 0.2),
            ("AI Agents", "AI Engineering", "GLOBAL", 0.20),
            ("AI Agents", "AI Engineering", "KE", 0.005),
        ]
    )
    series = months(
        [("AI Agents", f"2025-{m:02d}", 0.05) for m in range(1, 7)]
        + [("AI Agents", f"2026-{m:02d}", 0.20) for m in range(1, 7)]
    )
    out, _ = dif.build_diffusion(frame, series, settings, kenya_months_observed=5)
    assert out[out.skill == "AI Agents"].iloc[0]["confidence"] == "provisional"


# ------------------------------------------------------------------ trend


def test_trend_compares_smoothed_blocks_not_single_months():
    """A single quiet hiring month must not read as a trend."""
    series = months(
        [("X", f"2025-{m:02d}", 0.10) for m in range(1, 7)]
        + [("X", f"2026-{m:02d}", 0.20) for m in range(1, 7)]
    )
    trend = dif.global_trend(series, window=6)
    assert trend[trend.skill == "X"].iloc[0]["global_trend_12m"] == pytest.approx(0.10)


def test_missing_months_count_as_zero_not_missing():
    """The postings existed in those months; the skill just was not asked for."""
    series = months(
        [("X", "2026-01", 0.0)] * 1 + [("X", f"2026-{m:02d}", 0.12) for m in range(2, 13)]
    )
    trend = dif.global_trend(series, window=6)
    assert not trend.empty


def test_empty_month_frame_is_safe():
    assert dif.global_trend(pd.DataFrame()).empty


# ----------------------------------------------------------------- outputs


def test_watchlist_only_contains_teach_ahead(settings):
    frame = current(
        [
            ("SQL", "t", "GLOBAL", 0.2),
            ("SQL", "t", "KE", 0.2),
            ("Git", "t", "GLOBAL", 0.2),
            ("Git", "t", "KE", 0.2),
            ("Java", "t", "GLOBAL", 0.2),
            ("Java", "t", "KE", 0.2),
            ("AI Agents", "AI Engineering", "GLOBAL", 0.20),
            ("AI Agents", "AI Engineering", "KE", 0.005),
            ("Perl", "t", "GLOBAL", 0.002),
            ("Perl", "t", "KE", 0.001),
        ]
    )
    series = months(
        [("AI Agents", f"2025-{m:02d}", 0.05) for m in range(1, 7)]
        + [("AI Agents", f"2026-{m:02d}", 0.20) for m in range(1, 7)]
    )
    out, _ = dif.build_diffusion(frame, series, settings)
    wl = dif.watchlist(out)
    assert set(wl["status"]) == {dif.TEACH_AHEAD}


def test_track_summary_rolls_up_to_programmes(settings):
    """The whole point: counts must land on Zindua programmes, not stay as raw
    technology tallies."""
    frame = current(
        [
            ("SQL", "Data Analytics", "GLOBAL", 0.2),
            ("SQL", "Data Analytics", "KE", 0.2),
            ("Git", "Software Engineering Core", "GLOBAL", 0.2),
            ("Git", "Software Engineering Core", "KE", 0.2),
            ("Java", "Software Engineering Core", "GLOBAL", 0.2),
            ("Java", "Software Engineering Core", "KE", 0.2),
        ]
    )
    out, _ = dif.build_diffusion(frame, months([]), settings)
    summary = dif.track_summary(out)
    assert set(summary["zindua_track"]) == {"Data Analytics", "Software Engineering Core"}
    assert summary["skills"].sum() == 3


def test_empty_input_is_handled(settings):
    out, diag = dif.build_diffusion(pd.DataFrame(), pd.DataFrame(), settings)
    assert out.empty
    assert "error" in diag


# ----------------------------------------------------------- co-occurrence


def test_cooccurrence_lift_above_one_means_paired():
    job_skills = pd.DataFrame(
        [
            {"job_id": f"j{i}", "skill": s, "source_group": "GLOBAL"}
            for i in range(10)
            for s in (["React", "TypeScript"] if i < 8 else ["COBOL"])
        ]
    )
    out = fr.build_cooccurrence(job_skills, "GLOBAL", min_pairs=2)
    pair = out[(out.skill_a == "React") & (out.skill_b == "TypeScript")]
    assert not pair.empty
    assert pair.iloc[0]["lift"] > 1.0


# ------------------------------------------- Wayback replay de-duplication


def test_replayed_duplicate_of_a_live_posting_is_dropped(tmp_path):
    """A posting can be collected live and again from a Wayback snapshot.

    The replay adapter derives native_id from the original URL precisely so the
    two are recognisable as one posting. Keeping both would double-count it in
    the Kenyan segment -- the smallest, and so the most distortable.
    """
    import sqlite3

    from jobradar.store.db import JobStore
    from jobradar.store.models import GROUP_KE, RawJob

    db = tmp_path / "jobs.sqlite"
    store = JobStore(db)
    store.upsert_jobs(
        [
            RawJob(
                source="brightermonday",
                source_group=GROUP_KE,
                native_id="abc123",
                url="https://x/listings/abc123",
                title="Dev",
            ),
            RawJob(
                source="wayback_brightermonday",
                source_group=GROUP_KE,
                native_id="abc123",
                url="http://web.archive.org/…",
                title="Dev",
                is_historical=True,
            ),
            RawJob(
                source="wayback_brightermonday",
                source_group=GROUP_KE,
                native_id="only-archived",
                url="http://web.archive.org/…",
                title="Old Dev",
                is_historical=True,
            ),
        ]
    )
    store.close()

    jobs = fr.load_jobs(db)
    assert len(jobs) == 2, "the replayed copy of a live posting must be dropped"
    assert set(jobs["native_id"]) == {"abc123", "only-archived"}
    # The live row is the one kept.
    assert jobs.loc[jobs.native_id == "abc123", "source"].iloc[0] == "brightermonday"
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3


def test_history_mask_matches_wayback_board_variants():
    """Sources are named wayback_brightermonday, so the configured entry
    'wayback' must match by prefix. An exact match would silently exclude every
    backfilled posting -- the whole point of the phase."""
    frame = pd.DataFrame({"source": ["hn_hiring", "wayback_brightermonday", "greenhouse"]})
    mask = fr._history_mask(frame, {"hn_hiring", "wayback"})
    assert list(mask) == [True, True, False]
