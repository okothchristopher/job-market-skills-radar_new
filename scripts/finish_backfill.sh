#!/usr/bin/env bash
# Finish the Phase 5 backfill and refresh every downstream frame.
#
# Deliberately SEQUENTIAL. Running the Kenyan crawl and the Wayback backfill at
# the same time produced 624 ConnectionErrors against Fuzu -- every one of those
# URLs returned HTTP 200 in half a second when probed alone afterwards. Two
# crawlers competing for connections is not worth the wall-clock it saves.
set -u

cd "$(dirname "$0")/.." || exit 1
PY=.venv/Scripts/python.exe
JR=.venv/Scripts/jobradar.exe

echo "== waiting for the Wayback backfill queue to drain"
while :; do
  pending=$("$PY" -c "
import sqlite3
c = sqlite3.connect('data/raw/jobs.sqlite')
print(c.execute(\"SELECT COUNT(*) FROM crawl_queue WHERE source='wayback' AND state='pending'\").fetchone()[0])
")
  [ "$pending" -eq 0 ] && break
  echo "   $pending replay URLs pending"
  sleep 120
done

echo "== re-running discovery so every configured archive year is queued"
# The budget is per board PER YEAR. An earlier run allocated it per board, so
# the first year listed consumed the whole allowance and 2024 was never queried
# -- leaving only postings that had survived into 2025. Re-discovering here
# picks up the years that were starved.
"$JR" discover --sources wayback

echo "== recovering URLs lost to transient network faults"
"$JR" retry --sources wayback,fuzu,brightermonday,myjobmag
"$JR" fetch --sources wayback
"$JR" fetch --sources fuzu,brightermonday,myjobmag

echo "== re-running extraction over the enlarged corpus"
"$JR" extract

echo "== rebuilding the frames and the diffusion table"
"$JR" aggregate

echo "== done"
