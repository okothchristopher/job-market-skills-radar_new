"""The diffusion engine — global demand as a leading indicator for Kenya.

This is where the project's thesis gets tested rather than assumed: *skills that
rise globally trickle down to the Kenyan market after some lag*. If that holds,
Zindua can teach ahead of local demand instead of reacting to it.

Three things make the output trustworthy, and each exists because the naive
version is actively misleading.

**1. The gap must be calibrated.** Every global source skews ahead of the Kenyan
market — Greenhouse and Ashby are US-tech-heavy, Hacker News is startup-heavy,
the remote boards are modern-stack-heavy. Measured on the real corpus, **86% of
skills show a positive global-minus-Kenya gap**. That is not 86% of technologies
arriving in Kenya; it is the structural bias of the sources. So we estimate the
bias from skills known to be fully diffused (SQL, Git, Java, Excel, HTML, CSS,
Linux) and subtract their median. Without this, the "watchlist" would rank how
modern a technology is, which is not a curriculum question.

**2. Ranks beat absolute gaps.** Even calibrated, a percentage-point gap carries
more precision than the data supports. The watchlist is ordered by rank.

**3. What cannot be computed is left null.** ``estimated_lag_months`` and the
Kenyan trend need Kenyan *history*, and live boards delete expired postings — the
collected Kenyan corpus is entirely from 2026. Those columns stay null until
Wayback backfill (Phase 5) or a few months of monthly collection supply the
series. Inventing them from a single year would be the easiest way to produce a
confident, wrong answer.

Known limitation, stated plainly
--------------------------------
Without Kenyan history, ``teach_ahead`` and ``global_only`` cannot be fully
separated: both look like "high globally, absent locally". The discriminator we
have is the *global* trend — a skill rising globally and absent locally is a
plausible arrival, while one that is large-but-flat globally and still absent
locally has had time to arrive and has not. That is a proxy, not the real test,
and every such row is marked ``provisional``.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

import pandas as pd

# Status values. See PLAN.md section 6 for the curriculum meaning of each.
TEACH_AHEAD = "teach_ahead"
ARRIVING = "arriving"
ESTABLISHED_BOTH = "established_both"
KENYA_SPECIFIC = "kenya_specific"
GLOBAL_ONLY = "global_only"
DECLINING_BOTH = "declining_both"
# Small in both markets. We have ample data -- it is simply not in demand --
# so this must not be conflated with a thin sample.
LOW_DEMAND = "low_demand"
INSUFFICIENT = "insufficient_data"


@dataclass
class DiffusionSettings:
    calibration_skills: list[str]
    teach_ahead_min_global_share: float = 0.05
    teach_ahead_max_kenya_share: float = 0.02
    teach_ahead_min_global_trend: float = 0.01
    min_cell_size: int = 100
    enable_lag_estimate: bool = False
    min_months_for_lag: int = 12

    @classmethod
    def from_config(cls, config) -> DiffusionSettings:
        d = config.settings.get("diffusion", {})
        return cls(
            calibration_skills=list(d.get("calibration_skills", [])),
            teach_ahead_min_global_share=float(d.get("teach_ahead_min_global_share", 0.05)),
            teach_ahead_max_kenya_share=float(d.get("teach_ahead_max_kenya_share", 0.02)),
            teach_ahead_min_global_trend=float(d.get("teach_ahead_min_global_trend", 0.01)),
            min_cell_size=config.min_cell_size,
            enable_lag_estimate=bool(d.get("enable_lag_estimate", False)),
            min_months_for_lag=int(d.get("min_months_for_lag", 12)),
        )


def global_trend(skill_month: pd.DataFrame, window: int = 6) -> pd.DataFrame:
    """Year-over-year change in global share, per skill.

    Defined as the mean share over the trailing ``window`` months minus the mean
    over the ``window`` months before that. Smoothing over six-month blocks
    rather than comparing two single months keeps a quiet hiring month from
    reading as a trend.
    """
    if skill_month.empty:
        return pd.DataFrame(columns=["skill", "global_trend_12m", "months_observed"])

    g = skill_month[skill_month["source_group"] == "GLOBAL"]
    if g.empty:
        return pd.DataFrame(columns=["skill", "global_trend_12m", "months_observed"])

    months = sorted(g["month"].unique())
    recent = set(months[-window:])
    prior = set(months[-2 * window : -window])

    rows = []
    for skill, chunk in g.groupby("skill"):
        # Months where the skill is absent are genuine zeroes, not missing data:
        # the postings existed, they just did not ask for it.
        recent_vals = [
            chunk.loc[chunk["month"] == m, "pct_share"].sum() if m in set(chunk["month"]) else 0.0
            for m in recent
        ]
        prior_vals = [
            chunk.loc[chunk["month"] == m, "pct_share"].sum() if m in set(chunk["month"]) else 0.0
            for m in prior
        ]
        if not recent_vals or not prior_vals:
            continue
        rows.append(
            {
                "skill": skill,
                "global_trend_12m": statistics.fmean(recent_vals) - statistics.fmean(prior_vals),
                "months_observed": chunk["month"].nunique(),
            }
        )
    return pd.DataFrame(rows)


def _calibration_offset(frame: pd.DataFrame, calibration_skills: list[str]) -> tuple[float, int]:
    """Median raw gap across fully-diffused skills — our estimate of source bias."""
    baseline = frame[frame["skill"].isin(calibration_skills)]["diffusion_gap"].dropna()
    if len(baseline) < 3:
        # Too few baseline skills present to estimate the bias. Better to apply
        # no correction and say so than to correct by a number built on two rows.
        return 0.0, len(baseline)
    return float(baseline.median()), len(baseline)


def _classify(row, s: DiffusionSettings, has_kenya_history: bool) -> tuple[str, str]:
    """Return ``(status, confidence)`` for one skill.

    ``insufficient_data`` is reserved for cells too thin to judge. A skill that
    is simply small in both markets is ``low_demand`` — we have ample data on it,
    and calling that "insufficient" would hide a real answer behind a
    data-quality label.
    """
    g = row["global_share_now"]
    k = row["kenya_share_now"]
    gap = row["calibrated_gap"]
    trend = row.get("global_trend_12m")

    if pd.isna(g) or pd.isna(k) or not row.get("cell_usable", True):
        return INSUFFICIENT, "none"

    # Kenya ahead of global, after correcting for source bias. Local necessity
    # the global signal is structurally blind to (M-Pesa, USSD, Daraja).
    if gap is not None and not pd.isna(gap) and gap < -0.01:
        return KENYA_SPECIFIC, "medium"

    strong_global = g >= s.teach_ahead_min_global_share
    absent_locally = k <= s.teach_ahead_max_kenya_share
    rising = trend is not None and not pd.isna(trend) and trend >= s.teach_ahead_min_global_trend
    falling = trend is not None and not pd.isna(trend) and trend <= -s.teach_ahead_min_global_trend

    if strong_global and absent_locally:
        if rising:
            # Large and growing globally, absent locally: the arrival candidate.
            return TEACH_AHEAD, "medium" if has_kenya_history else "provisional"
        if falling:
            return DECLINING_BOTH, "low"
        # Large but flat globally and still absent locally: it has had time to
        # arrive and has not. Without Kenyan history this is inference, not
        # measurement.
        return GLOBAL_ONLY, "low" if has_kenya_history else "provisional"

    if strong_global and not absent_locally:
        return (ARRIVING, "medium") if rising else (ESTABLISHED_BOTH, "medium")

    if not absent_locally:
        # Present in Kenya without being large globally: already part of the
        # local market, whatever the world is doing.
        return ESTABLISHED_BOTH, "low"

    # Small globally and absent locally. Plenty of data; simply not in demand.
    return LOW_DEMAND, "medium"


def build_diffusion(
    skill_current: pd.DataFrame,
    skill_month: pd.DataFrame,
    settings: DiffusionSettings,
    kenya_months_observed: int = 0,
    skill_year: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Build the ``skill_diffusion`` frame plus a diagnostics dict.

    Args:
        skill_current: share by source_group, all sources (current state).
        skill_month: share by source_group and month, unbiased sources only.
        settings: thresholds and the calibration skill list.
        kenya_months_observed: how many distinct months the Kenyan corpus spans.
            Below ``min_months_for_lag`` the Kenyan trend and the lag estimate
            stay null rather than being fabricated.
    """
    if skill_current.empty:
        return pd.DataFrame(), {"error": "no current-state data"}

    pivot = (
        skill_current.pivot_table(
            index=["skill", "zindua_track", "category"],
            columns="source_group",
            values="pct_share",
            aggfunc="first",
        )
        .reset_index()
        .rename(columns={"GLOBAL": "global_share_now", "KE": "kenya_share_now"})
    )
    for column in ("global_share_now", "kenya_share_now"):
        if column not in pivot:
            pivot[column] = pd.NA
    # A skill absent from a segment scores zero there, not "unknown": the
    # postings were searched and it was not asked for.
    pivot["global_share_now"] = pivot["global_share_now"].fillna(0.0)
    pivot["kenya_share_now"] = pivot["kenya_share_now"].fillna(0.0)

    # Whether each segment's cell is thick enough to judge at all. Computed on
    # the comparable (technical-postings) denominator, so a market with many
    # postings but few technical ones is correctly treated as thin.
    usable = (
        skill_current.groupby("source_group")["usable"].max().to_dict()
        if "usable" in skill_current
        else {}
    )
    pivot["cell_usable"] = bool(usable.get("GLOBAL", True)) and bool(usable.get("KE", True))

    pivot["diffusion_gap"] = pivot["global_share_now"] - pivot["kenya_share_now"]

    offset, n_baseline = _calibration_offset(pivot, settings.calibration_skills)
    pivot["calibrated_gap"] = pivot["diffusion_gap"] - offset

    trend = global_trend(skill_month)
    pivot = pivot.merge(trend, on="skill", how="left")

    has_kenya_history = kenya_months_observed >= settings.min_months_for_lag

    # The Kenyan trend is computable once the archive backfill supplies a
    # series. It is measured over the SAME whole-year window as the global one:
    # the Kenyan series ends 2025-12 while the global runs into 2026, so
    # trailing-window trends would compare Kenya over 2025 against the world
    # over 2026 and call the difference diffusion.
    pivot["kenya_trend_12m"] = pd.NA
    if has_kenya_history and skill_year is not None and not skill_year.empty:
        ke = year_over_year(skill_year, "KE", 2024, 2025)
        if not ke.empty:
            pivot = pivot.merge(
                ke[["skill", "yoy_change"]].rename(columns={"yoy_change": "_ke_yoy"}),
                on="skill",
                how="left",
            )
            pivot["kenya_trend_12m"] = pivot["_ke_yoy"]
            pivot = pivot.drop(columns=["_ke_yoy"])

    # Still null: a cross-correlation lag estimate needs several years on both
    # sides. Two overlapping years cannot locate a lag, and the backtest below
    # shows why guessing would be wrong.
    pivot["estimated_lag_months"] = pd.NA

    classified = pivot.apply(lambda r: _classify(r, settings, has_kenya_history), axis=1)
    pivot["status"] = [c[0] for c in classified]
    pivot["confidence"] = [c[1] for c in classified]
    pivot["is_calibration_baseline"] = pivot["skill"].isin(settings.calibration_skills)

    pivot["gap_rank"] = pivot["calibrated_gap"].rank(ascending=False, method="min")

    columns = [
        "skill",
        "zindua_track",
        "category",
        "global_share_now",
        "kenya_share_now",
        "diffusion_gap",
        "calibrated_gap",
        "gap_rank",
        "global_trend_12m",
        "kenya_trend_12m",
        "estimated_lag_months",
        "status",
        "confidence",
        "is_calibration_baseline",
        "months_observed",
        "cell_usable",
    ]
    frame = pivot[[c for c in columns if c in pivot]].sort_values("calibrated_gap", ascending=False)

    diagnostics = {
        "calibration_offset": round(offset, 5),
        "calibration_skills_found": n_baseline,
        "calibration_applied": n_baseline >= 3,
        "kenya_months_observed": kenya_months_observed,
        "kenya_history_sufficient": has_kenya_history,
        "lag_estimate_enabled": settings.enable_lag_estimate and has_kenya_history,
        "skills": len(frame),
        "status_counts": frame["status"].value_counts().to_dict(),
        "positive_raw_gap_share": round(float((pivot["diffusion_gap"] > 0).mean()), 3),
    }
    return frame, diagnostics


def watchlist(diffusion: pd.DataFrame, limit: int = 25) -> pd.DataFrame:
    """The headline deliverable: skills to teach before Kenya asks for them."""
    if diffusion.empty:
        return diffusion
    return (
        diffusion[diffusion["status"] == TEACH_AHEAD]
        .sort_values(["calibrated_gap", "global_trend_12m"], ascending=False)
        .head(limit)
    )


def track_summary(diffusion: pd.DataFrame) -> pd.DataFrame:
    """Roll the classification up onto Zindua programmes."""
    if diffusion.empty:
        return diffusion
    summary = (
        diffusion.groupby("zindua_track")
        .agg(
            skills=("skill", "count"),
            teach_ahead=("status", lambda s: int((s == TEACH_AHEAD).sum())),
            established=("status", lambda s: int((s == ESTABLISHED_BOTH).sum())),
            kenya_specific=("status", lambda s: int((s == KENYA_SPECIFIC).sum())),
            global_only=("status", lambda s: int((s == GLOBAL_ONLY).sum())),
            mean_global_share=("global_share_now", "mean"),
            mean_kenya_share=("kenya_share_now", "mean"),
        )
        .reset_index()
    )
    return summary.sort_values("teach_ahead", ascending=False)


def year_over_year(
    skill_year: pd.DataFrame, source_group: str, from_year: int, to_year: int
) -> pd.DataFrame:
    """Change in share between two whole years, for one segment.

    Preferred over a trailing-window trend whenever both segments are being
    compared, because the windows must line up. The Kenyan series (backfilled
    from archives) runs 2024-01 to 2025-12 while the global series runs into
    2026; a trailing-6-month trend would therefore measure Kenya over 2025 and
    the world over 2026 and present the difference as diffusion.

    Absence in a year is a genuine zero: the postings existed and did not ask
    for the skill.
    """
    if skill_year.empty:
        return pd.DataFrame(columns=["skill", "share_from", "share_to", "yoy_change"])

    seg = skill_year[(skill_year["source_group"] == source_group) & skill_year["usable"]]
    if seg.empty:
        return pd.DataFrame(columns=["skill", "share_from", "share_to", "yoy_change"])

    wide = seg.pivot_table(index="skill", columns="year", values="pct_share", aggfunc="first")
    if from_year not in wide or to_year not in wide:
        return pd.DataFrame(columns=["skill", "share_from", "share_to", "yoy_change"])

    out = pd.DataFrame(
        {
            "skill": wide.index,
            "share_from": wide[from_year].fillna(0.0).to_numpy(),
            "share_to": wide[to_year].fillna(0.0).to_numpy(),
        }
    )
    out["yoy_change"] = out["share_to"] - out["share_from"]
    return out


def backtest_thesis(
    skill_year: pd.DataFrame,
    from_year: int = 2024,
    to_year: int = 2025,
    min_share: float = 0.01,
    rise_threshold: float = 0.005,
) -> dict:
    """Test the trickle-down thesis instead of assuming it (PLAN.md §11a).

    The claim under test: **skills that rise globally also rise in Kenya.** The
    curriculum strategy rests on it, so it gets measured.

    Reported as a hit rate — of the skills that rose globally over the window,
    what fraction also rose in Kenya — alongside the correlation between the two
    changes and a base rate. The base rate is what makes the hit rate meaningful:
    if 70% of *all* skills rose in Kenya, a 70% hit rate among global risers is
    no evidence of diffusion at all.
    """
    g = year_over_year(skill_year, "GLOBAL", from_year, to_year)
    k = year_over_year(skill_year, "KE", from_year, to_year)
    if g.empty or k.empty:
        return {"error": "insufficient history in one or both segments"}

    joined = g.merge(k, on="skill", suffixes=("_global", "_kenya"))
    # Skills too small in both markets carry no signal either way.
    joined = joined[
        (joined["share_to_global"] >= min_share) | (joined["share_to_kenya"] >= min_share)
    ]
    if len(joined) < 10:
        return {"error": f"only {len(joined)} comparable skills"}

    risers = joined[joined["yoy_change_global"] >= rise_threshold]
    hits = risers[risers["yoy_change_kenya"] > 0]
    base = joined[joined["yoy_change_kenya"] > 0]

    hit_rate = len(hits) / len(risers) if len(risers) else None
    base_rate = len(base) / len(joined)

    return {
        "window": f"{from_year} -> {to_year}",
        "comparable_skills": len(joined),
        "global_risers": len(risers),
        "risers_that_also_rose_in_kenya": len(hits),
        "hit_rate": round(hit_rate, 3) if hit_rate is not None else None,
        "base_rate_any_skill_rose_in_kenya": round(base_rate, 3),
        "lift_over_base_rate": round(hit_rate - base_rate, 3) if hit_rate is not None else None,
        "correlation": round(
            float(joined["yoy_change_global"].corr(joined["yoy_change_kenya"])), 3
        ),
        "hits": sorted(hits["skill"].tolist()),
        "misses": sorted(risers[risers["yoy_change_kenya"] <= 0]["skill"].tolist()),
    }
