"""Greenhouse public job board API.

The backbone of the global signal. One request per company returns that
employer's entire open board with full descriptions — Stripe's board is 615
postings in a single call. First-party employer data, so there is no aggregator
de-duplication or ranking layer between us and what the company actually wrote.

    https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true

``first_published`` is preferred over ``updated_at`` as the posting date. A board
refresh bumps ``updated_at``, which would smear every historical posting into the
current month and destroy the trend axis.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from ..fetch.client import Response
from ..parse.dates import parse_datetime
from ..parse.text import html_to_text, infer_remote
from ..store.models import GROUP_GLOBAL, RawJob
from .base import SourceAdapter, registry

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


@registry.register
class GreenhouseAdapter(SourceAdapter):
    name = "greenhouse"
    group = GROUP_GLOBAL

    def __init__(self, client, config=None, companies: Iterable[dict] | None = None) -> None:
        super().__init__(client, config)
        self.companies = list(companies or [])

    def discover(self) -> Iterator[str]:
        for company in self.companies:
            if company.get("ats") == "greenhouse":
                yield BOARD_URL.format(slug=company["slug"])

    @staticmethod
    def slug_from_url(url: str) -> str:
        return url.split("/boards/", 1)[1].split("/", 1)[0]

    def parse(self, response: Response) -> Iterable[RawJob]:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return []

        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            return []

        slug = self.slug_from_url(response.url)
        segment = next((c.get("segment") for c in self.companies if c.get("slug") == slug), None)

        out: list[RawJob] = []
        for job in jobs:
            parsed = self._parse_one(job, slug, segment)
            if parsed is not None:
                out.append(parsed)
        return out

    def _parse_one(self, job: dict, slug: str, segment: str | None) -> RawJob | None:
        native_id = str(job.get("id") or "").strip()
        title = (job.get("title") or "").strip()
        if not native_id or not title:
            return None

        location = (job.get("location") or {}).get("name")

        # Greenhouse returns departments and offices as lists of objects; a
        # posting may legitimately carry several of each.
        departments = [d.get("name") for d in (job.get("departments") or []) if d.get("name")]

        return RawJob(
            source=self.name,
            source_group=self.group,
            native_id=native_id,
            url=job.get("absolute_url") or "",
            title=title,
            description_html=job.get("content") or "",
            # company_name is often absent on the board endpoint; the board slug
            # is the reliable identity, and it feeds the cross-board dedupe key.
            company=job.get("company_name") or slug,
            location=location,
            is_remote=infer_remote(location, title),
            # first_published, not updated_at — see the module docstring.
            date_posted=parse_datetime(job.get("first_published") or job.get("updated_at")),
            valid_through=parse_datetime(job.get("application_deadline")),
            department=departments[0] if departments else None,
            extra={
                "board_slug": slug,
                "segment": segment,
                "departments": departments,
                "offices": [o.get("name") for o in (job.get("offices") or []) if o.get("name")],
                "requisition_id": job.get("requisition_id"),
                "updated_at": job.get("updated_at"),
            },
        )

    @staticmethod
    def description_text(job: RawJob) -> str:
        """Greenhouse double-encodes its HTML; ``html_to_text`` handles that."""
        return html_to_text(job.description_html)
