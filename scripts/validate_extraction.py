"""Measure skill-extraction precision on real postings.

This is the gate from PLAN.md section 8: **no trends are published until
precision is at least 0.90**, measured separately for the Kenyan and global
corpora.

Why separately? Extraction error that differs between the two segments would
masquerade as a diffusion gap. If we over-match a skill in global text and
under-match it in Kenyan text, the watchlist invents a trickle-down candidate
out of nothing but a regex bug.

Two modes:

``--audit``      Sample postings, run the extractor, and write every match to a
                 CSV with its evidence sentence for a human to mark. This is the
                 real measurement and it needs a person.

``--auto``       Approximate precision without a human, by re-checking each match
                 against a stricter confirmation rule. It cannot replace the
                 audit -- it shares the extractor's blind spots -- but it catches
                 gross regressions cheaply and runs in CI.

    python scripts/validate_extraction.py --auto
    python scripts/validate_extraction.py --audit --n 50 --out reports/audit.csv
"""

from __future__ import annotations

import argparse
import csv
import random
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobradar.config import Config
from jobradar.extract.skills import SkillExtractor
from jobradar.parse.text import html_to_text


# A match is auto-confirmed when the skill's own name appears near the match as a
# distinct token. This is deliberately stricter and dumber than the taxonomy
# pattern: it will not confirm alias-only matches (psql -> PostgreSQL), so the
# figure it produces is a LOWER bound on precision, never an inflated one.
def _confirm_token(skill_name: str) -> re.Pattern[str]:
    core = re.escape(skill_name.split("(")[0].strip())
    return re.compile(rf"(?<!\w){core}(?!\w)", re.IGNORECASE)


def sample_jobs(db: Path, group: str, n: int, seed: int) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    where = "source_group = 'KE'" if group == "KE" else "source_group = 'GLOBAL'"
    rows = conn.execute(
        f"SELECT job_id, source, title, description_html FROM jobs "
        f"WHERE {where} AND description_html IS NOT NULL AND LENGTH(description_html) > 400"
    ).fetchall()
    conn.close()
    random.Random(seed).shuffle(rows)
    return rows[:n]


def run_auto(extractor: SkillExtractor, db: Path, n: int, seed: int) -> int:
    print(f"Auto-validation on {n} postings per segment (seed {seed})\n")
    overall_ok = True

    for group in ("KE", "GLOBAL"):
        rows = sample_jobs(db, group, n, seed)
        if not rows:
            print(f"{group}: no postings collected yet -- skipped\n")
            continue

        total = confirmed = 0
        suspect: Counter[str] = Counter()
        per_skill: dict[str, list[int]] = defaultdict(lambda: [0, 0])

        for row in rows:
            text = html_to_text(row["description_html"])
            for match in extractor.extract(row["title"], text):
                total += 1
                per_skill[match.skill][1] += 1
                blob = f"{row['title']} {text}"
                if _confirm_token(match.skill).search(blob):
                    confirmed += 1
                    per_skill[match.skill][0] += 1
                else:
                    suspect[match.skill] += 1

        rate = confirmed / total if total else 0.0
        status = "PASS" if rate >= 0.90 else "REVIEW"
        print(f"{group}: {total:,} matches across {len(rows)} postings")
        print(f"  token-confirmed: {confirmed:,} ({rate:.1%})  [{status}]")
        if suspect:
            print("  alias-only or unconfirmed (expected for aliased skills):")
            for skill, count in suspect.most_common(8):
                print(f"    {skill:<22} {count:>5}")
        print()
        if rate < 0.90:
            overall_ok = False

    print(
        "NOTE: auto-validation is a lower bound and a regression guard only.\n"
        "      Alias matches (psql -> PostgreSQL, k8s -> Kubernetes) count as\n"
        "      unconfirmed here but are correct. The 0.90 gate in PLAN.md is the\n"
        "      --audit figure, which requires a human."
    )
    return 0 if overall_ok else 1


def run_audit(extractor: SkillExtractor, db: Path, n: int, seed: int, out: Path) -> int:
    """Write a hand-labelling sheet: one row per match, with evidence."""
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "segment",
                "job_id",
                "source",
                "title",
                "skill",
                "matched_in",
                "n_mentions",
                "evidence",
                "correct? (y/n)",
            ]
        )
        for group in ("KE", "GLOBAL"):
            for row in sample_jobs(db, group, n, seed):
                text = html_to_text(row["description_html"])
                for match in extractor.extract(row["title"], text):
                    writer.writerow(
                        [
                            group,
                            row["job_id"],
                            row["source"],
                            (row["title"] or "")[:80],
                            match.skill,
                            match.matched_in,
                            match.n_mentions,
                            (match.evidence[0] if match.evidence else "")[:220],
                            "",
                        ]
                    )
                    written += 1

    print(f"wrote {written:,} matches to {out}")
    print(
        "\nMark the last column y/n, then compute precision as y / (y + n) per\n"
        "segment. Publish no trends until both segments reach 0.90."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auto", action="store_true", help="cheap regression check")
    parser.add_argument("--audit", action="store_true", help="write a hand-labelling sheet")
    parser.add_argument("--n", type=int, default=50, help="postings per segment")
    parser.add_argument("--seed", type=int, default=20260906)
    parser.add_argument("--out", default="reports/extraction_audit.csv")
    args = parser.parse_args()

    config = Config.load()
    extractor = SkillExtractor.from_csv(config.path("taxonomy"))
    db = config.path("raw_db")
    print(f"taxonomy: {len(extractor.skills)} skills\n")

    if args.audit:
        return run_audit(extractor, db, args.n, args.seed, Path(args.out))
    return run_auto(extractor, db, args.n, args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
