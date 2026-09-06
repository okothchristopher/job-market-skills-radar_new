"""Remote-first job board APIs: Remotive, Arbeitnow, Jobicy, Himalayas.

These add breadth to the global signal. Each returns a snapshot of currently-open
remote roles in one or two requests, with full descriptions.

They are **current-snapshot only** — none exposes history — so they contribute to
``global_share_now`` but not to the historical series. That job belongs to Hacker
News.

Usage terms, which are real constraints rather than boilerplate
---------------------------------------------------------------
* **Remotive** advises a maximum of ~4 requests per day and states that excessive
  requests are blocked. It also prohibits republishing its listings to
  third-party job sites. Publishing aggregate skill statistics is within what it
  permits; republishing postings is not, and we never do. Reports drawing on it
  must credit Remotive with a link back.
* **Jobicy** asks to be clearly credited with a direct link to the source.

Both constraints are enforced through per-source cache TTLs in
``config/sources.yaml`` rather than left to discipline, and ``attribution()``
surfaces the required credits for the reporting layer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from urllib.parse import quote

from ..fetch.client import Response
from ..parse.dates import parse_datetime
from ..store.models import GROUP_GLOBAL, RawJob
from .base import SourceAdapter, registry


class _RemoteBoardAdapter(SourceAdapter):
    """Shared shape: one request, a list of jobs, no pagination worth chasing."""

    group = GROUP_GLOBAL
    endpoint: str = ""
    jobs_key: str = "jobs"

    def discover(self) -> Iterator[str]:
        yield self.config.get("endpoint", self.endpoint)

    def _jobs(self, response: Response) -> list[dict]:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return []
        if isinstance(payload, list):
            return payload
        jobs = payload.get(self.jobs_key)
        return jobs if isinstance(jobs, list) else []

    def parse(self, response: Response) -> Iterable[RawJob]:
        out: list[RawJob] = []
        for job in self._jobs(response):
            parsed = self._parse_one(job)
            if parsed is not None:
                out.append(parsed)
        return out

    def _parse_one(self, job: dict) -> RawJob | None:  # pragma: no cover - overridden
        raise NotImplementedError

    def attribution(self) -> str | None:
        """Credit line this source requires in any published report."""
        return self.config.get("attribution")


@registry.register
class RemotiveAdapter(_RemoteBoardAdapter):
    name = "remotive"
    endpoint = "https://remotive.com/api/remote-jobs"

    def _parse_one(self, job: dict) -> RawJob | None:
        native_id = str(job.get("id") or "")
        title = (job.get("title") or "").strip()
        if not native_id or not title:
            return None
        location = job.get("candidate_required_location")
        return RawJob(
            source=self.name,
            source_group=self.group,
            native_id=native_id,
            url=job.get("url") or "",
            title=title,
            description_html=job.get("description") or "",
            company=job.get("company_name"),
            location=location,
            is_remote=True,  # the entire board is remote roles
            date_posted=parse_datetime(job.get("publication_date")),
            employment_type=job.get("job_type"),
            industry=job.get("category"),
            extra={"tags": job.get("tags") or [], "salary_text": job.get("salary")},
        )


@registry.register
class ArbeitnowAdapter(_RemoteBoardAdapter):
    name = "arbeitnow"
    endpoint = "https://www.arbeitnow.com/api/job-board-api"
    jobs_key = "data"

    def _parse_one(self, job: dict) -> RawJob | None:
        native_id = (job.get("slug") or "").strip()
        title = (job.get("title") or "").strip()
        if not native_id or not title:
            return None
        return RawJob(
            source=self.name,
            source_group=self.group,
            native_id=native_id,
            url=job.get("url") or "",
            title=title,
            description_html=job.get("description") or "",
            company=job.get("company_name"),
            location=job.get("location"),
            is_remote=bool(job.get("remote")) if job.get("remote") is not None else None,
            date_posted=parse_datetime(job.get("created_at")),
            employment_type=(job.get("job_types") or [None])[0],
            extra={"tags": job.get("tags") or [], "job_types": job.get("job_types") or []},
        )


@registry.register
class JobicyAdapter(_RemoteBoardAdapter):
    name = "jobicy"
    endpoint = "https://jobicy.com/api/v2/remote-jobs?count=100"

    def _parse_one(self, job: dict) -> RawJob | None:
        native_id = str(job.get("id") or "")
        title = (job.get("jobTitle") or "").strip()
        if not native_id or not title:
            return None
        return RawJob(
            source=self.name,
            source_group=self.group,
            native_id=native_id,
            url=job.get("url") or "",
            title=title,
            description_html=job.get("jobDescription") or job.get("jobExcerpt") or "",
            company=job.get("companyName"),
            location=job.get("jobGeo"),
            is_remote=True,
            date_posted=parse_datetime(job.get("pubDate")),
            employment_type=(job.get("jobType") or [None])[0]
            if isinstance(job.get("jobType"), list)
            else job.get("jobType"),
            industry=(job.get("jobIndustry") or [None])[0]
            if isinstance(job.get("jobIndustry"), list)
            else job.get("jobIndustry"),
            salary_min=_as_float(job.get("salaryMin")),
            salary_max=_as_float(job.get("salaryMax")),
            salary_currency=job.get("salaryCurrency"),
            extra={"job_level": job.get("jobLevel"), "salary_period": job.get("salaryPeriod")},
        )


@registry.register
class HimalayasAdapter(_RemoteBoardAdapter):
    """Himalayas holds ~103,000 postings but serves only 20 per request.

    It is the one source here worth paginating: the others return their whole
    useful snapshot in a single call. Paging is cursor-based, so the next page's
    address only exists once the current one is fetched — hence ``next_urls``
    rather than enumerating in ``discover``.
    """

    name = "himalayas"
    endpoint = "https://himalayas.app/jobs/api?limit=100"
    # A cap, not a target. Left unbounded this would walk 5,000 pages and hammer
    # a host that has been perfectly cooperative.
    default_max_pages = 60

    def next_urls(self, response: Response) -> Iterable[str]:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return ()

        cursor = payload.get("nextCursor")
        if not cursor:
            return ()

        max_pages = int(self.config.get("max_pages", self.default_max_pages))
        page = _page_number(response.url)
        if page >= max_pages:
            return ()

        base = self.config.get("endpoint", self.endpoint).split("&cursor=")[0]
        return [f"{base}&cursor={quote(str(cursor), safe='')}&page={page + 1}"]

    def _parse_one(self, job: dict) -> RawJob | None:
        native_id = str(job.get("guid") or "")
        title = (job.get("title") or "").strip()
        if not native_id or not title:
            return None
        restrictions = job.get("locationRestrictions") or []
        location = ", ".join(r for r in restrictions if isinstance(r, str)) or None
        return RawJob(
            source=self.name,
            source_group=self.group,
            native_id=native_id,
            url=job.get("applicationLink") or "",
            title=title,
            description_html=job.get("description") or job.get("excerpt") or "",
            company=job.get("companyName"),
            location=location,
            is_remote=True,
            date_posted=parse_datetime(job.get("pubDate")),
            valid_through=parse_datetime(job.get("expiryDate")),
            employment_type=job.get("employmentType"),
            salary_min=_as_float(job.get("minSalary")),
            salary_max=_as_float(job.get("maxSalary")),
            salary_currency=job.get("currency"),
            extra={
                "seniority": job.get("seniority"),
                "categories": job.get("categories") or [],
                "company_slug": job.get("companySlug"),
            },
        )


def _page_number(url: str) -> int:
    """Page index encoded in a paginated URL; 1 for the first, unnumbered page."""
    match = re.search(r"[?&]page=(\d+)", url)
    return int(match.group(1)) if match else 1


def _as_float(value) -> float | None:
    """Salary fields arrive as numbers, numeric strings, empty strings or null."""
    if value in (None, "", 0):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
