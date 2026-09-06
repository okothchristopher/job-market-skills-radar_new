"""Core data structures shared across adapters.

``RawJob`` is the contract every source adapter returns. Adapters differ wildly —
JSON-LD scraped from HTML, a Greenhouse API payload, a Hacker News comment — but
they all normalise to this before anything downstream sees them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

# Sources are grouped rather than used individually in the diffusion analysis:
# the thesis is about global-vs-Kenya, not about any one board.
GROUP_KE = "KE"
GROUP_GLOBAL = "GLOBAL"
GROUP_REGION = "REGION"  # Uganda / Nigeria — collected under D2, analysed separately


@dataclass
class RawJob:
    """One job posting as collected, before enrichment.

    Attributes:
        source: Adapter name, e.g. ``brightermonday``.
        source_group: One of GROUP_KE / GROUP_GLOBAL / GROUP_REGION.
        native_id: The source's own identifier, used to build a stable job_id.
        url: Canonical public URL of the posting.
        title: Job title as advertised.
        description_html: Raw description markup, kept so text extraction can be
            re-run without re-fetching.
        date_posted: When the employer published it. This is the trend axis, and
            it is deliberately distinct from ``fetched_at`` — for Wayback-replayed
            postings the two can differ by years.
        is_historical: True when replayed from an archive rather than fetched live.
    """

    source: str
    source_group: str
    native_id: str
    url: str
    title: str
    description_html: str = ""
    company: str | None = None
    location: str | None = None
    country: str | None = None
    is_remote: bool | None = None
    date_posted: datetime | date | None = None
    valid_through: datetime | date | None = None
    employment_type: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    industry: str | None = None
    department: str | None = None
    fetched_at: datetime | None = None
    is_historical: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def job_id(self) -> str:
        """Stable identity for this posting.

        Derived from source and native id rather than content, so re-collecting the
        same posting updates a row instead of creating a duplicate.
        """
        return hashlib.sha1(f"{self.source}:{self.native_id}".encode()).hexdigest()[:16]

    @property
    def year(self) -> int | None:
        return self.date_posted.year if self.date_posted else None

    @property
    def month(self) -> str | None:
        """``YYYY-MM``, the granularity the diffusion lag estimate needs."""
        if not self.date_posted:
            return None
        return f"{self.date_posted.year:04d}-{self.date_posted.month:02d}"

    def dedupe_key(self) -> str:
        """Key for collapsing the same job posted to several boards.

        Deliberately coarse — normalised title plus company. Date proximity is
        applied as a second pass in ``extract.dedupe``, because two genuinely
        different openings with the same title at the same company months apart
        should not collapse into one.
        """
        title = re.sub(r"[^a-z0-9 ]+", " ", (self.title or "").lower())
        title = re.sub(r"\s+", " ", title).strip()
        company = re.sub(r"[^a-z0-9]+", "", (self.company or "").lower())
        return f"{title}|{company}"
