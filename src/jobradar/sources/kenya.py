"""Kenyan job board adapters: BrighterMonday, MyJobMag, Fuzu, JobWebKenya.

All four serve full server-side HTML with a schema.org ``JobPosting``, so no
browser automation is needed and the parsers key off structured data rather than
CSS selectors — a redesign rarely changes JSON-LD, because that data is what
feeds Google Jobs.

Crawl shape is the same everywhere: category pages fan out to detail pages, and
detail pages carry the posting. ``next_urls`` does the fan-out, so the queue
stays the single record of what is left and a killed crawl resumes.

**We never crawl keyword-search URLs.** BrighterMonday disallows every query
string except ``page=2``-``page=7``; MyJobMag disallows query strings entirely
but permits path pagination. Category crawling respects both and is cheaper
anyway — one request yields 16-27 postings instead of one answer about one skill.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import urljoin

from ..fetch.client import Response
from ..parse import jsonld
from ..parse.dates import parse_datetime
from ..parse.text import infer_remote
from ..store.models import GROUP_KE, GROUP_REGION, RawJob
from .base import SourceAdapter, registry


class _KenyanBoardAdapter(SourceAdapter):
    """Shared crawl and JSON-LD mapping for the Kenyan boards."""

    group = GROUP_KE
    base_url: str = ""
    country: str = "KE"
    # Regex matching a job detail URL for this site.
    detail_pattern: re.Pattern[str] = re.compile(r"$^")

    def is_detail_url(self, url: str) -> bool:
        return bool(self.detail_pattern.search(url))

    # ------------------------------------------------------------- discovery

    def category_urls(self) -> list[str]:
        categories = self.config.get("categories") or []
        return [urljoin(self.base_url, path) for path in categories]

    def discover(self) -> Iterator[str]:
        yield from self.category_urls()

    def detail_links(self, response: Response) -> list[str]:
        """Job detail URLs found on a listing page."""
        return sorted(
            {urljoin(self.base_url, href) for href in self.detail_pattern.findall(response.text)}
        )

    def pagination_urls(self, response: Response) -> list[str]:
        """Next listing pages. Empty by default; each site overrides.

        Implementations advance **one page at a time** and are only called when
        the current page actually yielded postings — see :meth:`next_urls`.
        """
        return []

    def next_urls(self, response: Response) -> Iterable[str]:
        # A detail page is a leaf: following links out of it would wander into
        # "similar jobs" and quietly triple the crawl.
        if self.is_detail_url(response.url):
            return ()

        details = self.detail_links(response)

        # Only ask for the next page if this one had postings on it. Fanning out
        # speculatively to a fixed page count means requesting pages that do not
        # exist: BrighterMonday's software-data category holds three pages, so a
        # blind 2..7 fan-out 404s four times and fills the failure table with our
        # own mistakes rather than real problems.
        pages = self.pagination_urls(response) if details or self.is_index_page(response) else []
        return [*details, *pages]

    def is_index_page(self, response: Response) -> bool:
        """True for a page that legitimately holds no postings but leads to them.

        Fuzu's country landing page is the case: it lists categories, not jobs,
        so an empty detail-link result there is expected rather than the end of
        pagination.
        """
        return False

    # --------------------------------------------------------------- parsing

    def parse(self, response: Response) -> Iterable[RawJob]:
        if not self.is_detail_url(response.url):
            return []  # listing page: links only, handled by next_urls
        job = self.parse_detail(response)
        return [job] if job is not None else []

    def parse_detail(self, response: Response) -> RawJob | None:
        posting, index = jsonld.find_job_posting(response.text)
        if posting is None:
            return None

        title = (posting.get("title") or "").strip()
        if not title:
            return None

        location = jsonld.location_text(posting, index)
        is_remote = jsonld.remote_flag(posting)
        applicant_location = jsonld.applicant_location(posting, index)
        if is_remote is None:
            is_remote = infer_remote(location, title)

        salary_min, salary_max, currency = jsonld.salary(posting, index)

        return RawJob(
            source=self.name,
            source_group=self.group,
            native_id=self.native_id(response.url, posting),
            url=response.url,
            title=title,
            description_html=posting.get("description") or "",
            company=jsonld.organization_name(posting, index),
            # A remote posting often has no jobLocation at all; the applicant
            # location requirement is then the only geography stated.
            location=location or applicant_location,
            country=self.country,
            is_remote=is_remote,
            date_posted=parse_datetime(posting.get("datePosted")),
            valid_through=parse_datetime(posting.get("validThrough")),
            employment_type=jsonld.employment_type(posting),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            industry=posting.get("industry") if isinstance(posting.get("industry"), str) else None,
            department=posting.get("occupationalCategory")
            if isinstance(posting.get("occupationalCategory"), str)
            else None,
            fetched_at=datetime.now(UTC),
            extra={"applicant_location": applicant_location},
        )

    def native_id(self, url: str, posting: dict) -> str:
        """Stable per-site identifier. The URL slug is the reliable one."""
        return url.rstrip("/").rsplit("/", 1)[-1]


@registry.register
class BrighterMondayAdapter(_KenyanBoardAdapter):
    """BrighterMonday Kenya.

    robots.txt is the binding constraint here: it disallows ``/job/``, ``/api/``
    and **every** query string, then re-allows ``page=2`` through ``page=7``
    explicitly. So pagination stops at 7 by configuration rather than by
    discovering a 403 — generating a disallowed URL and letting the gate reject
    it would just fill the failure table with our own mistakes.
    """

    name = "brightermonday"
    base_url = "https://www.brightermonday.co.ke"
    detail_pattern = re.compile(r"https://www\.brightermonday\.co\.ke/listings/[a-z0-9-]+")

    def pagination_urls(self, response: Response) -> list[str]:
        """Advance one page at a time, stopping at the robots ceiling.

        robots.txt re-allows only ``page=2``-``page=7`` against a blanket
        query-string ban, so 7 is a hard limit rather than a tuning knob.
        Because this is only called when the current page yielded postings, a
        category with fewer pages stops on its own instead of 404-ing.
        """
        max_page = int(self.config.get("max_list_page", 7))
        match = re.search(r"[?&]page=(\d+)", response.url)
        current = int(match.group(1)) if match else 1
        if current >= max_page:
            return []
        base = response.url.split("?", 1)[0]
        return [f"{base}?page={current + 1}"]


@registry.register
class MyJobMagAdapter(_KenyanBoardAdapter):
    """MyJobMag Kenya.

    robots.txt disallows every query string (``/*?``), but path pagination
    (``/jobs-by-field/information-technology/2``) is permitted, so that is how we
    page.
    """

    name = "myjobmag"
    base_url = "https://www.myjobmag.co.ke"
    detail_pattern = re.compile(r"/job/[a-z0-9][a-z0-9-]{8,}")

    def detail_links(self, response: Response) -> list[str]:
        hrefs = re.findall(r'href="(/job/[a-z0-9][a-z0-9-]{8,})"', response.text)
        return sorted({urljoin(self.base_url, h) for h in hrefs})

    def pagination_urls(self, response: Response) -> list[str]:
        max_pages = int(self.config.get("max_pages", 10))
        # Path pagination: /category, /category/2, /category/3 ...
        page_match = re.search(r"/(\d+)$", response.url)
        if page_match:
            current = int(page_match.group(1))
            if current >= max_pages:
                return []
            base = response.url.rsplit("/", 1)[0]
            return [f"{base}/{current + 1}"]
        return [f"{response.url.rstrip('/')}/2"]


@registry.register
class FuzuAdapter(_KenyanBoardAdapter):
    """Fuzu.

    Job URLs are ``/{country}/jobs/{slug}`` (plural) while categories are
    ``/{country}/job/{slug}`` (singular) — easy to conflate, and conflating them
    yields a crawl of category pages that finds no postings at all.

    Category pages carry an ``ItemList`` JSON-LD listing each posting's name and
    URL, which is more reliable than scraping anchors.

    Fuzu's gzipped sitemaps sit behind a Cloudflare challenge, so category pages
    are the way in. Under decision D2 we also collect Uganda and Nigeria, tagged
    ``REGION`` so Kenya can be analysed on its own.
    """

    name = "fuzu"
    base_url = "https://www.fuzu.com"
    detail_pattern = re.compile(r"/[a-z]+/jobs/[a-z0-9-]{8,}")

    # Seniority and location filters share the category URL shape but are not
    # categories; crawling them just re-walks the same postings.
    NON_CATEGORY: ClassVar[set[str]] = {
        "basic",
        "middle",
        "senior",
        "nairobi",
        "mombasa",
        "kisumu",
        "nakuru",
        "thika",
    }

    def _countries(self) -> list[str]:
        return list(self.config.get("countries") or ["kenya"])

    def group_for(self, url: str) -> str:
        return GROUP_KE if "/kenya/" in url else GROUP_REGION

    def country_for(self, url: str) -> str:
        match = re.search(r"fuzu\.com/([a-z]+)/", url)
        code = {"kenya": "KE", "uganda": "UG", "nigeria": "NG"}
        return code.get(match.group(1) if match else "", "KE")

    def discover(self) -> Iterator[str]:
        """Country landing pages; categories are discovered from them."""
        for country in self._countries():
            yield f"{self.base_url}/{country}/job"

    def detail_links(self, response: Response) -> list[str]:
        links: set[str] = set()
        # Prefer the ItemList: it is the site's own declaration of what is on
        # the page, and it survives markup changes.
        for block in jsonld.iter_blocks(response.text):
            if isinstance(block, dict) and block.get("@type") == "ItemList":
                for item in block.get("itemListElement") or []:
                    url = item.get("url") if isinstance(item, dict) else None
                    if isinstance(url, str) and "/jobs/" in url:
                        links.add(url)
        for href in re.findall(r'href="(/[a-z]+/jobs/[a-z0-9-]{8,})"', response.text):
            links.add(urljoin(self.base_url, href))
        return sorted(links)

    def is_index_page(self, response: Response) -> bool:
        return bool(re.search(r"/[a-z]+/job/?$", response.url))

    def pagination_urls(self, response: Response) -> list[str]:
        """Category links from a landing page, plus paging within a category."""
        out: set[str] = set()

        if re.search(r"/[a-z]+/job/?$", response.url):
            # Landing page: fan out to categories.
            for href in re.findall(r'href="(/[a-z]+/job/[a-z0-9-]+)"', response.text):
                slug = href.rstrip("/").rsplit("/", 1)[-1]
                if slug not in self.NON_CATEGORY:
                    out.add(urljoin(self.base_url, href))
            return sorted(out)

        max_pages = int(self.config.get("max_pages", 5))
        match = re.search(r"[?&]page=(\d+)", response.url)
        current = int(match.group(1)) if match else 0
        if current + 1 < max_pages:
            base = response.url.split("?", 1)[0]
            out.add(f"{base}?page={current + 1}")
        return sorted(out)

    def parse_detail(self, response: Response) -> RawJob | None:
        job = super().parse_detail(response)
        if job is not None:
            # Kenya is the analysis target; Uganda and Nigeria are collected
            # under D2 but kept in their own group.
            job.source_group = self.group_for(response.url)
            job.country = self.country_for(response.url)
        return job


@registry.register
class JobWebKenyaAdapter(_KenyanBoardAdapter):
    """JobWebKenya.

    **This source declares ``Crawl-delay: 60``** — one request per minute, 24x
    slower than our default. The client honours it automatically, which means a
    full crawl here is measured in hours, not minutes.

    That is a real cost for a source whose market coverage overlaps heavily with
    BrighterMonday and MyJobMag, so it ships with a conservative per-run page
    budget and accumulates across the monthly runs rather than being drained in
    one sitting. Set ``enabled: false`` if the wall-clock is not worth it.
    """

    name = "jobwebkenya"
    base_url = "https://jobwebkenya.com"
    detail_pattern = re.compile(r"https://jobwebkenya\.com/jobs/[a-z0-9-]{8,}/")

    def pagination_urls(self, response: Response) -> list[str]:
        max_pages = int(self.config.get("max_pages", 3))
        page_match = re.search(r"/page/(\d+)/?$", response.url)
        if page_match:
            current = int(page_match.group(1))
            if current >= max_pages:
                return []
            base = response.url.rsplit("/page/", 1)[0]
            return [f"{base}/page/{current + 1}/"]
        return [f"{response.url.rstrip('/')}/page/2/"]
