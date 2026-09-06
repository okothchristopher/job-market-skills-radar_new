"""Curriculum briefs — one per Zindua programme.

Turns the diffusion table into decisions a curriculum committee can act on:
what to add, expand, hold, watch and retire, per programme.

Sizing a programme by the *mean* of its skills' shares is wrong, and wrong in a
way that penalises exactly the programmes worth defending. Cybersecurity Core
has nine skills that each sit between 1% and 4.6% of technical postings, so its
mean looks negligible — yet a free-text probe finds security language in **5.0%
of all global postings**. Its demand is real but fragmented across many named
skills, where a track like Data Analytics concentrates demand in a few big ones.

So a programme is sized by **coverage**: the share of postings asking for *at
least one* skill it teaches. That is the question a prospective student is
really asking — what fraction of the market wants someone who did this course.

What the recommendations rest on
--------------------------------
The backtest in ``diffusion.backtest_thesis`` found that global demand and
Kenyan demand move **together**, weakly (Spearman +0.25, +15pp lift over base
rate), and that applying a one-year lag makes prediction *worse* (Spearman
-0.20). So these briefs do not claim a skill will arrive in Kenya on a
schedule. ``add`` means a large and growing global level gap, which is a
strategic bet on a market that is different rather than merely delayed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..aggregate import diffusion as dif

# What to do about each skill, given its diffusion status.
ACTION_BY_STATUS = {
    dif.TEACH_AHEAD: "add",
    dif.ARRIVING: "expand",
    dif.ESTABLISHED_BOTH: "hold",
    dif.KENYA_SPECIFIC: "hold",
    dif.GLOBAL_ONLY: "watch",
    dif.DECLINING_BOTH: "retire",
    dif.LOW_DEMAND: "minor",
    dif.INSUFFICIENT: "unknown",
}

ACTION_MEANING = {
    "add": "Large and growing global gap. A strategic bet, not a scheduled arrival.",
    "expand": "Rising in both markets — the safest place to invest teaching time.",
    "hold": "Core demand, already present locally. Keep as is.",
    "watch": "Big globally, flat, still absent locally. Do not teach on global signal alone.",
    "retire": "Falling in both markets. Candidate for removal.",
    "minor": "Small in both markets. Ample data; simply not in demand.",
    "unknown": "Sample too thin to judge.",
}


@dataclass
class TrackBrief:
    track: str
    global_coverage: float
    kenya_coverage: float
    n_skills: int
    actions: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def coverage_gap(self) -> float:
        return self.global_coverage - self.kenya_coverage

    def top(self, action: str, n: int = 6) -> list[dict]:
        return self.actions.get(action, [])[:n]


def track_coverage(job_skills: pd.DataFrame, jobs: pd.DataFrame) -> pd.DataFrame:
    """Share of technical postings asking for at least one skill in each track.

    The denominator is technical postings (those mentioning any taxonomy skill),
    which is what makes the Kenyan and global figures comparable — Kenyan boards
    carry every sector, global ATS boards carry almost only tech.
    """
    rows = []
    for group in ("GLOBAL", "KE"):
        seg = job_skills[job_skills["source_group"] == group]
        if seg.empty:
            continue
        tech_total = seg["job_id"].nunique()
        for track, chunk in seg.groupby("zindua_track"):
            rows.append(
                {
                    "zindua_track": track,
                    "source_group": group,
                    "n_jobs": chunk["job_id"].nunique(),
                    "n_tech_jobs": tech_total,
                    "coverage": chunk["job_id"].nunique() / tech_total,
                }
            )
    return pd.DataFrame(rows)


def build_briefs(diffusion: pd.DataFrame, coverage: pd.DataFrame) -> list[TrackBrief]:
    """One brief per programme, ordered by how large its Kenyan market is."""
    if diffusion.empty:
        return []

    cov = coverage.pivot_table(
        index="zindua_track", columns="source_group", values="coverage", aggfunc="first"
    ).fillna(0.0)

    briefs: list[TrackBrief] = []
    for track, chunk in diffusion.groupby("zindua_track"):
        actions: dict[str, list[dict]] = {}
        for _, row in chunk.sort_values("calibrated_gap", ascending=False).iterrows():
            action = ACTION_BY_STATUS.get(row["status"], "unknown")
            actions.setdefault(action, []).append(
                {
                    "skill": row["skill"],
                    "global_share": float(row["global_share_now"]),
                    "kenya_share": float(row["kenya_share_now"]),
                    "calibrated_gap": float(row["calibrated_gap"]),
                    "global_trend": _maybe_float(row.get("global_trend_12m")),
                    "kenya_trend": _maybe_float(row.get("kenya_trend_12m")),
                    "status": row["status"],
                    "confidence": row.get("confidence", ""),
                }
            )
        briefs.append(
            TrackBrief(
                track=track,
                global_coverage=float(cov.loc[track, "GLOBAL"]) if track in cov.index else 0.0,
                kenya_coverage=float(cov.loc[track, "KE"]) if track in cov.index else 0.0,
                n_skills=len(chunk),
                actions=actions,
            )
        )

    # Ordered by Kenyan market size: the programmes with the most local demand
    # are the ones a Nairobi school is deciding about first.
    return sorted(briefs, key=lambda b: -b.kenya_coverage)


def _maybe_float(value):
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def to_markdown(briefs: list[TrackBrief], backtest: dict, diagnostics: dict) -> str:
    """Render the briefs as a committee-readable document."""
    out: list[str] = []
    out.append("# Curriculum briefs — what the job market is asking for\n")
    out.append(
        "Generated from {skills} skills across {n:,} job postings. Shares are the "
        "percentage of **technical postings** — those mentioning at least one skill "
        "in the taxonomy — because Kenyan job boards carry every sector while the "
        "global sources are almost entirely technology companies.\n".format(
            skills=diagnostics.get("skills", "?"),
            n=diagnostics.get("total_postings", 0),
        )
    )

    out.append("## Read this before the numbers\n")
    hit = backtest.get("hit_rate")
    base = backtest.get("base_rate_any_skill_rose_in_kenya")
    if hit is not None and base is not None:
        out.append(
            f"The premise that global demand *predicts* Kenyan demand a year ahead is "
            f"**not supported by this data**. Of the skills that rose globally in "
            f"{backtest.get('window', '2024 -> 2025')}, {hit:.0%} also rose in Kenya — but "
            f"{base:.0%} of *all* skills rose in Kenya over the same period, so the lift "
            f"is only {backtest.get('lift_over_base_rate', 0):+.1%}. Testing it with a "
            "one-year lag makes prediction actively worse.\n"
        )
    out.append(
        "What the data does show is **large, persistent level gaps** between the two "
        "markets that are not closing on a yearly timescale. Kenya looks like a "
        "*different* market, not a delayed one. Read `add` below as a strategic bet on "
        "that gap, not as a schedule.\n"
    )

    out.append("## Programmes by Kenyan market size\n")
    out.append("| Programme | Kenyan coverage | Global coverage | Gap | Skills |")
    out.append("|---|---:|---:|---:|---:|")
    for b in briefs:
        out.append(
            f"| {b.track} | {b.kenya_coverage:.1%} | {b.global_coverage:.1%} | "
            f"{b.coverage_gap:+.1%} | {b.n_skills} |"
        )
    out.append(
        "\n*Coverage = share of technical postings asking for at least one skill the "
        "programme teaches. This is deliberately not the mean of its skills' shares: "
        "a programme whose demand is spread across many small skills — Cybersecurity "
        "is the clearest case — would otherwise look negligible.*\n"
    )

    for b in briefs:
        out.append(f"\n## {b.track}\n")
        out.append(
            f"**{b.kenya_coverage:.1%}** of Kenyan technical postings, "
            f"**{b.global_coverage:.1%}** globally, across {b.n_skills} tracked skills.\n"
        )
        for action in ("add", "expand", "hold", "watch", "retire"):
            items = b.top(action)
            if not items:
                continue
            out.append(f"**{action.upper()}** — {ACTION_MEANING[action]}\n")
            out.append("| Skill | Global | Kenya | Global trend | Kenya trend |")
            out.append("|---|---:|---:|---:|---:|")
            for it in items:
                gt = "n/a" if it["global_trend"] is None else f"{it['global_trend']:+.1%}"
                kt = "n/a" if it["kenya_trend"] is None else f"{it['kenya_trend']:+.1%}"
                out.append(
                    f"| {it['skill']} | {it['global_share']:.1%} | "
                    f"{it['kenya_share']:.1%} | {gt} | {kt} |"
                )
            out.append("")
    return "\n".join(out)
