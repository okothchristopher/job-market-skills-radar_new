"""Wayback Machine replay — the only source of Kenyan history.

Live job boards delete expired postings. The Kenyan corpus collected in Phase 3
is **entirely from 2026**, which is why the diffusion engine has to leave
``kenya_trend_12m`` and ``estimated_lag_months`` null and mark every
``teach_ahead`` row provisional. This adapter is what fixes that.

How it works
------------
1. The **CDX API** enumerates archived snapshots of a board's listing URLs::

       http://web.archive.org/cdx/search/cdx?url=brightermonday.co.ke/listings/*
           &from=2025&to=2025&output=json&collapse=urlkey&filter=statuscode:200

2. The **``id_`` replay endpoint** returns the originally-archived bytes rather
   than the page wrapped in the archive's toolbar::

       http://web.archive.org/web/{timestamp}id_/{original_url}

3. Those bytes still contain the site's JSON-LD ``JobPosting``, so the *same*
   parser used for the live board reads them. Verified end to end: a 2025
   snapshot yielded title, company, description and ``datePosted``.

The key subtlety
----------------
**Archive date and posting date are different things.** A page crawled in May
2025 can carry a ``datePosted`` from 2024, because the posting was already old
when the archive found it. So we deliberately do *not* filter by archive year to
get a posting year — we replay what the archive holds and read the real
``datePosted`` off each page. This is how 2024 data surfaces at all, given that
direct 2024 crawls of these listing URLs number in the single digits.

Cost and courtesy
-----------------
archive.org is slower and stricter than the live boards, so it gets its own
1 req/s bucket. Roughly 19,800 BrighterMonday listings were archived in 2025;
replaying all of them would take over five hours, so each board has a per-run
URL budget and the crawl accumulates across runs. The queue makes that safe —
a run killed halfway resumes rather than restarting.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime

from ..fetch.client import FetchError, Response
from ..fetch.robots import RobotsDisallowed
from ..store.models import RawJob
from .base import SourceAdapter, registry
from .kenya import BrighterMondayAdapter, MyJobMagAdapter

log = logging.getLogger(__name__)

CDX_URL = "http://web.archive.org/cdx/search/cdx"
REPLAY_URL = "http://web.archive.org/web/{timestamp}id_/{url}"

# Which live adapter parses each board's archived HTML, plus the CDX query shape.
#
# `cdx_filter` is a regex applied by the CDX API itself, and it is load-bearing
# for MyJobMag: CDX prefix-matches and sorts alphabetically, so a plain
# "myjobmag.co.ke/job/*" returns thousands of "/job-application/" apply forms
# first -- they sort before "/job/" -- and any sane row limit truncates before a
# single real posting appears. Filtering server-side is the difference between
# 520 postings and zero.
REPLAYABLE = {
    "brightermonday": {
        "adapter": BrighterMondayAdapter,
        "cdx_url": "brightermonday.co.ke*",
        "cdx_filter": r"original:.*brightermonday\.co\.ke/listings/.*",
    },
    "myjobmag": {
        "adapter": MyJobMagAdapter,
        "cdx_url": "myjobmag.co.ke*",
        "cdx_filter": r"original:.*myjobmag\.co\.ke/job/.*",
    },
}

_REPLAY_RE = re.compile(r"/web/(\d{4,14})(?:id_)?/(https?://.+)$")


@registry.register
class WaybackAdapter(SourceAdapter):
    """Replays archived Kenyan listings through their live parsers."""

    name = "wayback"
    group = "KE"

    def __init__(self, client, config=None) -> None:
        super().__init__(client, config)
        self._parsers = {board: spec["adapter"](client, {}) for board, spec in REPLAYABLE.items()}

    # ------------------------------------------------------------- discovery

    def boards(self) -> list[str]:
        configured = self.config.get("replays") or list(REPLAYABLE)
        return [b for b in configured if b in REPLAYABLE]

    def years(self) -> list[int]:
        return [int(y) for y in (self.config.get("years") or [2025])]

    def discover(self) -> Iterator[str]:
        budget = int(self.config.get("max_urls_per_board", 3000))
        for board in self.boards():
            found = 0
            for year in self.years():
                if found >= budget:
                    break
                for original, timestamp in self._cdx(board, year, budget - found):
                    # Enforced here as well as via the CDX `limit`, because the
                    # server is free to return more rows than asked for and the
                    # budget is what keeps a run bounded.
                    if found >= budget:
                        break
                    found += 1
                    yield REPLAY_URL.format(timestamp=timestamp, url=original)
            log.info("wayback: %s -> %d archived snapshots", board, found)

    def _cdx(self, board: str, year: int, limit: int) -> list[tuple[str, str]]:
        """Enumerate archived snapshots for one board and archive year.

        ``collapse=urlkey`` keeps one snapshot per URL rather than every crawl of
        the same page, which is what makes the budget buy distinct postings
        instead of repeats.
        """
        spec = REPLAYABLE[board]
        url = (
            f"{CDX_URL}?url={spec['cdx_url']}"
            f"&from={year}&to={year}&output=json"
            f"&fl=original,timestamp&collapse=urlkey"
            f"&filter=statuscode:200&filter={spec['cdx_filter']}"
            f"&limit={limit}"
        )
        try:
            response = self.client.get(url)
            rows = json.loads(response.text)
        except (FetchError, RobotsDisallowed, json.JSONDecodeError) as exc:
            log.warning("wayback CDX failed for %s %s: %s", board, year, exc)
            return []

        if not rows or len(rows) < 2:
            return []

        # CDX prefix-matches, so "myjobmag.co.ke/job/*" also returns
        # "/job-application/722315" -- an apply form, not a posting. Validate
        # each URL against the live adapter's own detail pattern rather than
        # spending a replay request to discover it is not a job.
        parser = self._parsers[board]
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        skipped = duplicates = 0

        for row in rows[1:]:  # first row is the header when fl= is supplied
            if len(row) < 2:
                continue
            original, timestamp = row[0], row[1]

            if not parser.is_detail_url(original):
                skipped += 1
                continue

            # collapse=urlkey does not collapse tracking parameters, so the same
            # posting appears once per campaign it was shared under
            # (...-wpdgm4 and ...-wpdgm4?utm_source=KenyaMOJA.com). Left alone
            # these burn the replay budget on duplicates of pages we already have.
            canonical = original.split("?", 1)[0].rstrip("/")
            if canonical in seen:
                duplicates += 1
                continue
            seen.add(canonical)
            out.append((canonical, timestamp))

        if skipped or duplicates:
            log.info(
                "wayback: %s %s -- %d non-postings, %d tracking duplicates skipped",
                board,
                year,
                skipped,
                duplicates,
            )
        return out

    # --------------------------------------------------------------- parsing

    @staticmethod
    def original_url(replay_url: str) -> str | None:
        match = _REPLAY_RE.search(replay_url)
        return match.group(2) if match else None

    def board_for(self, original_url: str) -> str | None:
        for board, spec in REPLAYABLE.items():
            domain = spec["cdx_url"].rstrip("*")
            if domain in original_url:
                return board
        return None

    def is_detail_url(self, url: str) -> bool:
        return "/web/" in url

    def parse(self, response: Response) -> Iterable[RawJob]:
        original = self.original_url(response.url)
        if not original:
            return []
        board = self.board_for(original)
        if board is None:
            return []

        parser = self._parsers[board]

        # Hand the parser a response carrying the ORIGINAL url, so the native_id
        # it derives from the slug matches what the live crawl produces. That is
        # what lets a re-collected posting be recognised as the same posting
        # rather than counted twice.
        as_live = Response(
            url=original,
            status_code=response.status_code,
            headers=response.headers,
            body=response.body,
            encoding=response.encoding,
            from_cache=response.from_cache,
        )
        job = parser.parse_detail(as_live)
        if job is None:
            return []

        # Provenance is kept in the source name: the analysis layer must be able
        # to tell replayed history from a live snapshot, and `unbiased_history_
        # sources` matches on the "wayback" prefix.
        job.source = f"wayback_{board}"
        job.is_historical = True
        job.fetched_at = datetime.now(UTC)
        job.extra = {
            **(job.extra or {}),
            "replayed_board": board,
            "archive_timestamp": (_REPLAY_RE.search(response.url) or [None, ""])[1]
            if _REPLAY_RE.search(response.url)
            else None,
            "archive_url": response.url,
        }
        return [job]
