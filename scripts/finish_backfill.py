"""Finish the Wayback backfill and refresh every downstream frame.

Written in Python rather than shell deliberately. The bash version broke on
Windows: git's autocrlf rewrote it with CRLF endings, and the carriage returns
inside an embedded Python heredoc left bash hunting for a closing quote. The
wait loop ran, the six steps that mattered did not, and the failure surfaced
only as "unexpected EOF" after an hour of waiting.

The sequence is deliberately SEQUENTIAL. Running the Kenyan crawl and the
Wayback backfill concurrently produced 624 ConnectionErrors against Fuzu --
every one of those URLs returned HTTP 200 in half a second when probed alone
afterwards. Two crawlers competing for connections is not worth the wall-clock.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOBRADAR = ROOT / ".venv" / "Scripts" / "jobradar.exe"
DB = ROOT / "data" / "raw" / "jobs.sqlite"


def pending(source: str = "wayback") -> int:
    with sqlite3.connect(DB) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM crawl_queue WHERE source = ? AND state = 'pending'",
            (source,),
        ).fetchone()[0]


def run(*args: str) -> int:
    print(f"\n$ jobradar {' '.join(args)}", flush=True)
    result = subprocess.run([str(JOBRADAR), *args], cwd=ROOT)
    if result.returncode != 0:
        print(f"  !! exited {result.returncode}", flush=True)
    return result.returncode


def wait_for_drain(poll_seconds: int = 60) -> None:
    while (n := pending()) > 0:
        print(f"   {n} replay URLs pending", flush=True)
        time.sleep(poll_seconds)


def main() -> int:
    print("== waiting for any in-flight replay queue to drain", flush=True)
    wait_for_drain()

    # The replay budget is per board PER YEAR. An earlier run allocated it per
    # board, so the first year listed consumed the whole allowance and 2024 was
    # never queried -- leaving only postings that had survived into 2025. This
    # re-discovery picks up the years that were starved.
    print("\n== re-discovering so every configured archive year is queued", flush=True)
    run("discover", "--sources", "wayback")

    print("\n== recovering URLs lost to transient network faults", flush=True)
    run("retry", "--sources", "wayback,fuzu,brightermonday,myjobmag")

    print("\n== draining the replay queue", flush=True)
    run("fetch", "--sources", "wayback")

    print("\n== recollecting the Kenyan boards", flush=True)
    run("fetch", "--sources", "fuzu,brightermonday,myjobmag")

    print("\n== re-running extraction over the enlarged corpus", flush=True)
    run("extract")

    print("\n== rebuilding the frames and the diffusion table", flush=True)
    run("aggregate")

    print("\n== done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
