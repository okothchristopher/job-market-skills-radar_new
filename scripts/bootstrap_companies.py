"""Probe candidate ATS slugs and write the confirmed ones to config/companies.yaml.

Run once to bootstrap, then re-run periodically — companies change ATS, and a
stale board slug is a silent hole in the global signal rather than a loud error.

Strategy
--------
Greenhouse is probed first, using the *metadata* endpoint (no ``content=true``),
which returns a few KB instead of several MB. Only candidates that fail on
Greenhouse are then tried on Ashby, which roughly halves the request count.

A slug is confirmed only on a 200 that actually parses and contains postings. A
board that exists but is empty is recorded separately: it is not a bad slug, but
it contributes nothing until it has openings again.

    python scripts/bootstrap_companies.py --limit 50   # smoke test
    python scripts/bootstrap_companies.py              # full run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobradar.config import Config
from jobradar.fetch.client import FetchError, build_client
from jobradar.fetch.robots import RobotsDisallowed
from jobradar.sources.candidates import all_candidates

GREENHOUSE_PROBE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
ASHBY_PROBE = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def probe_greenhouse(client, slug: str) -> tuple[bool, int]:
    """Return (confirmed, job_count)."""
    try:
        response = client.get(GREENHOUSE_PROBE.format(slug=slug))
    except (FetchError, RobotsDisallowed):
        return False, 0
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return False, 0
    jobs = payload.get("jobs")
    if jobs is None:
        return False, 0
    return True, len(jobs)


def probe_ashby(client, slug: str) -> tuple[bool, int]:
    try:
        response = client.get(ASHBY_PROBE.format(slug=slug))
    except (FetchError, RobotsDisallowed):
        return False, 0
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return False, 0
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return False, 0
    return True, len(jobs)


def _load_existing(path: Path) -> dict[str, list[dict]]:
    """Read a previous run's output so --merge can skip already-probed slugs."""
    if not path.exists():
        return {"companies": [], "empty_boards": []}
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        "companies": data.get("companies") or [],
        "empty_boards": data.get("empty_boards") or [],
        "not_found": data.get("not_found") or [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="probe only the first N candidates")
    parser.add_argument("--out", default="config/companies.yaml")
    parser.add_argument(
        "--min-jobs",
        type=int,
        default=1,
        help="boards with fewer open postings than this are recorded but disabled",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help=(
            "keep results already in --out and probe only slugs not listed there. "
            "Misses are not cached (a 404 raises before the cache writes), so "
            "without this a re-run re-probes every previously-rejected slug."
        ),
    )
    args = parser.parse_args()

    config = Config.load()
    client = build_client(
        cache_path=str(config.path("http_cache")),
        cache_ttl_seconds=config.cache_ttl_seconds,
        default_rate=config.defaults.get("rate_per_second", 0.5),
        domain_rates=config.domain_rates(),
        user_agent=config.user_agent,
    )

    candidates = all_candidates()

    confirmed: list[dict] = []
    empty: list[dict] = []
    misses: list[str] = []

    if args.merge:
        existing = _load_existing(Path(args.out))
        confirmed = existing["companies"]
        empty = existing["empty_boards"]
        # Misses must be remembered too. A 404 raises before the cache writes, so
        # a slug that failed last time is re-probed on every subsequent merge
        # unless we record it — which is most of the candidate list.
        misses = list(existing["not_found"])
        known = {r["slug"] for r in confirmed} | {r["slug"] for r in empty} | set(misses)
        before = len(candidates)
        candidates = [(slug, seg) for slug, seg in candidates if slug not in known]
        print(
            f"merge: {len(confirmed)} confirmed + {len(empty)} empty + "
            f"{len(misses)} known misses; probing {len(candidates)} of {before} candidates"
        )

    if args.limit:
        candidates = candidates[: args.limit]

    started = time.time()

    for index, (slug, segment) in enumerate(candidates, start=1):
        ats = None
        count = 0

        found, count = probe_greenhouse(client, slug)
        if found:
            ats = "greenhouse"
        else:
            found, count = probe_ashby(client, slug)
            if found:
                ats = "ashby"

        record = {"slug": slug, "ats": ats, "segment": segment, "open_jobs": count}
        if not found:
            misses.append(slug)
        elif count >= args.min_jobs:
            confirmed.append(record)
        else:
            empty.append(record)

        if index % 25 == 0 or index == len(candidates):
            elapsed = time.time() - started
            print(
                f"  [{index:>4}/{len(candidates)}] "
                f"confirmed={len(confirmed)} empty={len(empty)} miss={len(misses)} "
                f"({elapsed:.0f}s)",
                flush=True,
            )

    confirmed.sort(key=lambda r: (-r["open_jobs"], r["slug"]))

    by_ats = Counter(r["ats"] for r in confirmed)
    by_segment = Counter(r["segment"] for r in confirmed)
    total_jobs = sum(r["open_jobs"] for r in confirmed)

    lines = [
        "# Confirmed ATS job boards, generated by scripts/bootstrap_companies.py.",
        "#",
        "# Every slug here returned a valid job list from the live API at generation",
        "# time. Re-run the script periodically: companies change ATS, and a stale slug",
        "# is a silent hole in the global signal rather than a loud error.",
        "#",
        f"# Generated:      {time.strftime('%Y-%m-%d')}",
        f"# Candidates:     {len(candidates)}",
        f"# Confirmed:      {len(confirmed)}",
        f"# Empty boards:   {len(empty)} (valid slug, no open postings)",
        f"# Open postings:  {total_jobs:,}",
        "#",
        "# By ATS:     " + ", ".join(f"{k}={v}" for k, v in by_ats.most_common()),
        "# By segment: " + ", ".join(f"{k}={v}" for k, v in by_segment.most_common()),
        "",
        "companies:",
    ]
    for record in confirmed:
        lines.append(
            f"  - slug: {record['slug']}\n"
            f"    ats: {record['ats']}\n"
            f"    segment: {record['segment']}\n"
            f"    open_jobs: {record['open_jobs']}"
        )

    if empty:
        lines += [
            "",
            "# Valid boards with no open postings at generation time. Kept so a later",
            "# run can pick them up without re-probing the whole candidate list.",
            "empty_boards:",
        ]
        for record in sorted(empty, key=lambda r: r["slug"]):
            lines.append(
                f"  - slug: {record['slug']}\n"
                f"    ats: {record['ats']}\n"
                f"    segment: {record['segment']}"
            )

    if misses:
        lines += [
            "",
            "# Slugs that returned nothing on either ATS. Recorded so --merge can skip",
            "# them: a 404 raises before the response cache writes, so without this list",
            "# every future merge re-probes every rejected slug -- the majority of the",
            "# candidate set. Delete an entry to have it re-probed (companies do migrate",
            "# onto Greenhouse and Ashby).",
            "not_found:",
        ]
        for slug in sorted(set(misses)):
            lines.append(f"  - {slug}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\nwrote {out_path}")
    print(f"  confirmed boards : {len(confirmed)}")
    print(f"  empty boards     : {len(empty)}")
    print(f"  misses           : {len(misses)}")
    print(f"  open postings    : {total_jobs:,}")
    print(f"  by ATS           : {dict(by_ats)}")
    print(f"  by segment       : {dict(by_segment)}")
    print(f"\nclient stats: {client.stats.as_dict()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
