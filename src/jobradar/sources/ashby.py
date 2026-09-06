"""Ashby public job board API.

The second global backbone, skewing towards startups and scale-ups where
Greenhouse skews larger. Same shape: one request per company returns the whole
board.

    https://api.ashbyhq.com/posting-api/job-board/{board}

Ashby is the friendliest payload of any source here — it supplies
``descriptionPlain`` alongside the HTML, a real ``isRemote`` boolean rather than
a string we have to infer from, and a clean ``publishedAt``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from ..fetch.client import Response
from ..parse.dates import parse_datetime
from ..parse.text import infer_remote
from ..store.models import GROUP_GLOBAL, RawJob
from .base import SourceAdapter, registry

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


@registry.register
class AshbyAdapter(SourceAdapter):
    name = "ashby"
    group = GROUP_GLOBAL

    def __init__(self, client, config=None, companies: Iterable[dict] | None = None) -> None:
        super().__init__(client, config)
        self.companies = list(companies or [])

    def discover(self) -> Iterator[str]:
        for company in self.companies:
            if company.get("ats") == "ashby":
                yield BOARD_URL.format(slug=company["slug"])

    @staticmethod
    def slug_from_url(url: str) -> str:
        return url.split("/job-board/", 1)[1].split("?", 1)[0]

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

        # isListed false means the posting exists but is not publicly advertised;
        # counting it would overstate demand.
        if job.get("isListed") is False:
            return None

        location = job.get("location")
        is_remote = job.get("isRemote")
        if is_remote is None:
            is_remote = infer_remote(location, job.get("workplaceType"), title)

        return RawJob(
            source=self.name,
            source_group=self.group,
            native_id=native_id,
            url=job.get("jobUrl") or "",
            # descriptionPlain is already clean text; keeping the HTML would mean
            # re-deriving what Ashby has handed us.
            description_html=job.get("descriptionHtml") or "",
            title=title,
            company=slug,
            location=location,
            is_remote=bool(is_remote) if is_remote is not None else None,
            date_posted=parse_datetime(job.get("publishedAt")),
            employment_type=job.get("employmentType"),
            department=job.get("department"),
            extra={
                "board_slug": slug,
                "segment": segment,
                "team": job.get("team"),
                "workplace_type": job.get("workplaceType"),
                "description_plain": job.get("descriptionPlain") or "",
                "secondary_locations": [
                    loc.get("location")
                    for loc in (job.get("secondaryLocations") or [])
                    if isinstance(loc, dict)
                ],
            },
        )
