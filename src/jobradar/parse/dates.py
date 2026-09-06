"""Date parsing and normalisation.

Dates are this project's analysis axis, so two rules are enforced here rather
than left to each adapter:

**Everything is timezone-aware UTC.** Sources return a mix — Greenhouse sends
``-04:00`` offsets, BrighterMonday sends ``Z``, Hacker News sends epoch seconds.
Mixing naive and aware datetimes raises on comparison, and worse, mis-buckets
postings near month boundaries, which is exactly where a diffusion lag estimate
would read the difference.

**Implausible dates are rejected, not stored.** A posting dated 1970 or 2049 is a
parse artefact. Left in, it creates a phantom year cell that survives the
thin-cell filter only by accumulating other artefacts.
"""

from __future__ import annotations

from datetime import UTC, datetime

from dateutil import parser as dateparser

# Anything outside this window is a parse artefact rather than a real posting.
# The upper bound is generous: boards legitimately post roles months ahead.
MIN_YEAR = 2015
MAX_YEAR = 2030


def parse_datetime(value) -> datetime | None:
    """Parse a date from any source format into timezone-aware UTC.

    Accepts ISO strings, epoch seconds (int or float), and existing datetimes.
    Returns None for anything unparseable or implausible, rather than raising —
    one malformed date should not abort a crawl of thousands of postings.
    """
    if value is None or value == "":
        return None

    parsed: datetime | None = None

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    else:
        try:
            parsed = dateparser.parse(str(value))
        except (ValueError, TypeError, OverflowError):
            return None

    if parsed is None:
        return None

    # Naive values are assumed UTC. Sources that omit an offset are reporting a
    # date, not a precise instant, so the assumption costs at most a few hours.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)

    if not (MIN_YEAR <= parsed.year <= MAX_YEAR):
        return None

    return parsed


def year_month(value: datetime | None) -> tuple[int | None, str | None]:
    """Return ``(year, "YYYY-MM")`` — the trend and lag axes."""
    if value is None:
        return None, None
    return value.year, f"{value.year:04d}-{value.month:02d}"
