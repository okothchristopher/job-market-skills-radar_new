"""Tidy frames built from the raw store.

Two rules from PLAN.md are enforced here rather than left to the notebooks,
because both are easy to forget and expensive to get wrong:

**Rule 1 — share, never raw counts.** Corpus size differs wildly between years,
segments and sources. Python's raw count triples between 2024 and 2026 mostly
because we hold three times as many 2026 postings. Every metric is
``pct_share`` — the fraction of postings *in that cell* mentioning the skill.

**Rule 2 — thin cells are suppressed.** A cell built on fewer postings than
``analysis.min_cell_size`` gets ``usable = False``. Downstream code filters on
that flag rather than re-deriving the rule, so a trend can never quietly rest on
nine postings.

A third rule applies only to time series: **ATS boards are excluded from the
monthly and yearly frames.** They carry real publish dates, but a 2024 posting
still open in 2026 is an evergreen or hard-to-fill role, so their history
measures recruiting difficulty rather than demand. Only sources listed in
``analysis.unbiased_history_sources`` feed a trend.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from ..config import Config


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def load_jobs(db_path: str | Path) -> pd.DataFrame:
    """Every posting, without the description bodies."""
    with _connect(db_path) as conn:
        return pd.read_sql_query(
            "SELECT job_id, source, source_group, country, title, company, "
            "is_remote, year, month, seniority_placeholder FROM ("
            "  SELECT job_id, source, source_group, country, title, company, "
            "  is_remote, year, month, NULL AS seniority_placeholder FROM jobs"
            ")",
            conn,
        ).drop(columns=["seniority_placeholder"])


def load_job_skills(db_path: str | Path) -> pd.DataFrame:
    """One row per (posting, skill), joined to the posting's segment and date."""
    with _connect(db_path) as conn:
        return pd.read_sql_query(
            "SELECT s.job_id, s.skill, s.canonical_name, s.category, s.zindua_track, "
            "s.n_mentions, s.matched_in, j.source, j.source_group, j.year, j.month "
            "FROM job_skills s JOIN jobs j ON j.job_id = s.job_id",
            conn,
        )


def _totals(jobs: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    return jobs.groupby(keys, dropna=True).size().reset_index(name="n_jobs_total")


def build_skill_share(
    jobs: pd.DataFrame,
    job_skills: pd.DataFrame,
    keys: list[str],
    min_cell_size: int,
) -> pd.DataFrame:
    """Skill share for whatever ``keys`` define a cell, on three denominators.

    The denominator choice is not cosmetic — it decides whether the global and
    Kenyan numbers mean the same thing.

    Measured on the real corpus: **80.3% of global postings mention a technical
    skill, against 27.2% of Kenyan ones.** That is not a skills gap. The global
    corpus is ATS boards belonging to technology companies, so nearly every
    posting is a tech role; Kenyan job boards carry every sector, so most
    postings are nursing, driving, sales and teaching. Dividing by *all*
    postings therefore dilutes every Kenyan tech skill by roughly 3x and
    manufactures a diffusion gap out of corpus composition alone.

    So three shares are produced:

    ``pct_of_all``
        Fraction of every posting in the cell. Honest for "how much of this
        market wants X", useless for comparing two differently-composed markets.
    ``pct_of_tech``
        Fraction of postings that mention at least one taxonomy skill. This is
        the comparable one, and what the diffusion engine uses.
    ``share_of_mentions``
        Fraction of all skill mentions in the cell. Also normalises for how
        *specifically* a market writes its ads — Kenyan tech postings name fewer
        technologies each, which is a finding in its own right.
    """
    if jobs.empty:
        return pd.DataFrame()

    totals = _totals(jobs, keys)

    tech_jobs = (
        job_skills.groupby(keys, dropna=True)["job_id"].nunique().reset_index(name="n_tech_jobs")
    )
    slots = job_skills.groupby(keys, dropna=True).size().reset_index(name="n_skill_slots")

    mentions = (
        job_skills.groupby([*keys, "skill", "zindua_track", "category"], dropna=True)["job_id"]
        .nunique()
        .reset_index(name="n_jobs_mentioning")
    )

    frame = (
        mentions.merge(totals, on=keys, how="left")
        .merge(tech_jobs, on=keys, how="left")
        .merge(slots, on=keys, how="left")
    )

    frame["pct_of_all"] = frame["n_jobs_mentioning"] / frame["n_jobs_total"]
    frame["pct_of_tech"] = frame["n_jobs_mentioning"] / frame["n_tech_jobs"]
    frame["share_of_mentions"] = frame["n_jobs_mentioning"] / frame["n_skill_slots"]
    # pct_share stays as the canonical metric name used downstream; it is the
    # comparable denominator, not the raw one.
    frame["pct_share"] = frame["pct_of_tech"]

    # Thinness is judged on the comparable denominator too: a cell with 5,000
    # postings but 40 technical ones cannot support a claim about tech skills.
    frame["usable"] = frame["n_tech_jobs"] >= min_cell_size
    return frame.sort_values([*keys, "pct_share"], ascending=[*([True] * len(keys)), False])


def build_skill_year(config: Config, jobs: pd.DataFrame, job_skills: pd.DataFrame) -> pd.DataFrame:
    """Skill share by (source_group, year), restricted to unbiased history sources."""
    history = set(config.settings.get("analysis", {}).get("unbiased_history_sources", []))
    j = jobs[jobs["source"].isin(history) & jobs["year"].notna()]
    s = job_skills[job_skills["source"].isin(history) & job_skills["year"].notna()]
    return build_skill_share(j, s, ["source_group", "year"], config.min_cell_size)


def build_skill_month(config: Config, jobs: pd.DataFrame, job_skills: pd.DataFrame) -> pd.DataFrame:
    """Skill share by (source_group, month) — the series a lag estimate needs."""
    history = set(config.settings.get("analysis", {}).get("unbiased_history_sources", []))
    j = jobs[jobs["source"].isin(history) & jobs["month"].notna()]
    s = job_skills[job_skills["source"].isin(history) & job_skills["month"].notna()]
    return build_skill_share(j, s, ["source_group", "month"], config.min_cell_size)


def build_skill_current(
    config: Config, jobs: pd.DataFrame, job_skills: pd.DataFrame
) -> pd.DataFrame:
    """Current-state share by segment, across **all** sources.

    This is the one frame where ATS boards belong: they are a rich snapshot of
    what is being asked for right now, and survivorship bias only distorts the
    *time* dimension, which this frame does not have.
    """
    return build_skill_share(jobs, job_skills, ["source_group"], config.min_cell_size)


def build_cooccurrence(
    job_skills: pd.DataFrame, source_group: str, min_pairs: int = 5, top_n: int = 400
) -> pd.DataFrame:
    """Which skills are asked for together, with lift.

    Lift above 1 means the pair appears together more than independence would
    predict — that is the interesting signal for curriculum design, because it
    says which skills should be taught in the same module.
    """
    subset = job_skills[job_skills["source_group"] == source_group]
    if subset.empty:
        return pd.DataFrame()

    n_jobs = subset["job_id"].nunique()
    counts = subset.groupby("skill")["job_id"].nunique()
    keep = counts.nlargest(top_n).index
    subset = subset[subset["skill"].isin(keep)]

    # Pair up skills within each posting.
    by_job = subset.groupby("job_id")["skill"].apply(lambda s: sorted(set(s)))
    pairs: dict[tuple[str, str], int] = {}
    for skills in by_job:
        for i, a in enumerate(skills):
            for b in skills[i + 1 :]:
                pairs[(a, b)] = pairs.get((a, b), 0) + 1

    rows = []
    for (a, b), n in pairs.items():
        if n < min_pairs:
            continue
        p_a, p_b, p_ab = counts[a] / n_jobs, counts[b] / n_jobs, n / n_jobs
        rows.append(
            {
                "source_group": source_group,
                "skill_a": a,
                "skill_b": b,
                "n_jobs": n,
                "lift": round(p_ab / (p_a * p_b), 3) if p_a and p_b else None,
            }
        )
    frame = pd.DataFrame(rows)
    return frame.sort_values("n_jobs", ascending=False) if not frame.empty else frame


def export(frames: dict[str, pd.DataFrame], out_dir: str | Path) -> dict[str, str]:
    """Write each frame as CSV (readable, diffable) and Parquet (typed, fast)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for name, frame in frames.items():
        if frame is None or frame.empty:
            continue
        csv_path = out / f"{name}.csv"
        frame.to_csv(csv_path, index=False)
        written[name] = str(csv_path)
        try:
            frame.to_parquet(out / f"{name}.parquet", index=False)
        except Exception:  # pyarrow missing or a dtype it cannot handle
            pass
    return written
