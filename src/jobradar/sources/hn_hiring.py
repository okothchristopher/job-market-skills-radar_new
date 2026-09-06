"""Hacker News "Ask HN: Who is hiring?" — the historical spine.

Every month since long before 2024, the ``whoishiring`` account posts one thread
and employers reply with a job each. Retrieved through the Algolia HN API this
gives an **unbroken monthly global series**, free and without rate-limit risk —
the only such corpus found while planning. Live job boards delete expired
postings; this thread never does.

Two requests per month of history: one to find the thread, one to pull its whole
comment tree.

    https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring
    https://hn.algolia.com/api/v1/items/{id}

The posting convention is a pipe-delimited header line::

    Modash.io | Senior Product Engineer | Remote (Europe) | Full-time | €75k-€110k | url

It is a *convention*, not a schema — plenty of posts deviate. The parser takes
what it can and leaves the rest null rather than forcing a match, because a
wrongly-parsed company name would corrupt the cross-board dedupe key.

**Known bias, stated rather than hidden:** HN skews heavily towards startups,
remote-first companies and modern stacks. That is precisely why it must not be
the only global source — it is balanced by the Greenhouse/Ashby enterprise
boards, and the calibration baseline in PLAN.md section 6 corrects for what
remains.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime

from ..fetch.client import FetchError, Response
from ..parse.dates import parse_datetime
from ..parse.text import html_to_text, infer_remote
from ..store.models import GROUP_GLOBAL, RawJob
from .base import SourceAdapter, registry

SEARCH_URL = (
    "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=100"
)
ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"

_TITLE_RE = re.compile(r"who\s+is\s+hiring", re.IGNORECASE)
# "Who wants to be hired?" and "Freelancer? Seeking freelancer?" are the sibling
# threads posted by the same account. They are candidates advertising themselves,
# not employers hiring, and counting them would invert the signal.
_EXCLUDE_TITLE_RE = re.compile(r"wants?\s+to\s+be\s+hired|freelancer", re.IGNORECASE)

_REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)
_ONSITE_RE = re.compile(r"\b(onsite|on-site|in[- ]office)\b", re.IGNORECASE)


@registry.register
class HNHiringAdapter(SourceAdapter):
    name = "hn_hiring"
    group = GROUP_GLOBAL

    def __init__(self, client, config=None) -> None:
        super().__init__(client, config)
        self.since = str((config or {}).get("since", "2024-01"))

    # ------------------------------------------------------------- discovery

    def discover(self) -> Iterator[str]:
        """Yield item URLs for every monthly thread at or after ``since``."""
        for thread_id, _ in self.find_threads():
            yield ITEM_URL.format(id=thread_id)

    def find_threads(self) -> list[tuple[int, str]]:
        """Return ``(story_id, title)`` for each qualifying monthly thread."""
        try:
            response = self.client.get(SEARCH_URL)
            payload = response.json()
        except (FetchError, json.JSONDecodeError):
            return []

        threads: list[tuple[int, str]] = []
        for hit in payload.get("hits", []):
            title = hit.get("title") or ""
            if not _TITLE_RE.search(title) or _EXCLUDE_TITLE_RE.search(title):
                continue
            created = parse_datetime(hit.get("created_at"))
            if created is None:
                continue
            if f"{created.year:04d}-{created.month:02d}" < self.since:
                continue
            object_id = hit.get("objectID")
            if object_id:
                threads.append((int(object_id), title))
        return sorted(threads)

    # ---------------------------------------------------------------- parsing

    def parse(self, response: Response) -> Iterable[RawJob]:
        try:
            thread = response.json()
        except json.JSONDecodeError:
            return []

        thread_title = thread.get("title") or ""
        if _EXCLUDE_TITLE_RE.search(thread_title):
            return []

        thread_month = self._thread_month(thread_title, thread.get("created_at"))

        out: list[RawJob] = []
        # Only top-level children are job posts. Their replies are questions and
        # discussion, and counting them would double-count the parent's stack.
        for comment in thread.get("children") or []:
            job = self._parse_comment(comment, thread, thread_month)
            if job is not None:
                out.append(job)
        return out

    @staticmethod
    def _thread_month(title: str, created_at) -> str | None:
        parsed = parse_datetime(created_at)
        if parsed:
            return f"{parsed.year:04d}-{parsed.month:02d}"
        return None

    def _parse_comment(self, comment: dict, thread: dict, thread_month: str | None):
        text_html = comment.get("text")
        if not text_html:
            return None  # deleted or empty

        text = html_to_text(text_html)
        if len(text) < 80:
            return None  # meta-comments and one-liners, not job posts

        header = text.split("\n", 1)[0]
        company, location, employment_type = self._parse_header(header)

        native_id = str(comment.get("id") or "")
        if not native_id:
            return None

        # The comment's own timestamp is the posting date, and it is exact —
        # this is why HN gives a clean monthly series where boards give none.
        posted = parse_datetime(comment.get("created_at")) or parse_datetime(
            comment.get("created_at_i")
        )

        blob = f"{header} {location or ''}"
        is_remote = True if _REMOTE_RE.search(blob) else infer_remote(blob)
        if is_remote is None and _ONSITE_RE.search(blob):
            is_remote = False

        return RawJob(
            source=self.name,
            source_group=self.group,
            native_id=native_id,
            url=f"https://news.ycombinator.com/item?id={native_id}",
            title=self._parse_title(header) or (company or "Unspecified role"),
            description_html=text_html,
            company=company,
            location=location,
            is_remote=is_remote,
            date_posted=posted,
            employment_type=employment_type,
            fetched_at=datetime.now(UTC),
            extra={
                "thread_id": thread.get("id"),
                "thread_title": thread.get("title"),
                "thread_month": thread_month,
                "author": comment.get("author"),
                "header": header[:300],
            },
        )

    @staticmethod
    def _parse_header(header: str) -> tuple[str | None, str | None, str | None]:
        """Pull company, location and employment type from the pipe-delimited line.

        Returns None for any field the post does not clearly supply. Guessing
        would be worse than a null — company feeds the dedupe key.
        """
        if "|" not in header:
            return None, None, None

        parts = [p.strip() for p in header.split("|")]
        company = parts[0] or None
        if company and (len(company) > 80 or company.lower().startswith("http")):
            company = None

        location = None
        employment_type = None
        for part in parts[1:]:
            lowered = part.lower()
            if employment_type is None and re.search(
                r"\b(full[- ]?time|part[- ]?time|contract|intern|freelance)\b", lowered
            ):
                employment_type = part
            elif location is None and re.search(
                r"\b(remote|onsite|on-site|hybrid)\b|,\s*[A-Z]{2}\b", part, re.IGNORECASE
            ):
                location = part
        return company, location, employment_type

    @staticmethod
    def _parse_title(header: str) -> str | None:
        """The role is conventionally the second pipe-delimited field."""
        if "|" not in header:
            return None
        parts = [p.strip() for p in header.split("|")]
        if len(parts) < 2:
            return None
        candidate = parts[1]
        if not candidate or len(candidate) > 120 or candidate.lower().startswith("http"):
            return None
        return candidate
