"""Date parsing tests.

Dates are the analysis axis. A posting in the wrong month is a posting in the
wrong diffusion bucket, so these are stricter than they look.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobradar.parse.dates import parse_datetime, year_month


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-06T05:30:16.146337Z",  # BrighterMonday JSON-LD
        "2026-09-06",  # bare date
        "2026-09-06T05:30:16+00:00",  # Ashby
        datetime(2026, 9, 6, tzinfo=UTC),
    ],
)
def test_common_formats_parse_to_utc(value):
    parsed = parse_datetime(value)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.year == 2026 and parsed.month == 9


def test_offset_dates_are_converted_not_stripped():
    """Greenhouse sends -04:00. Stripping rather than converting would shift a
    late-evening posting into the previous day, and at a month boundary into the
    previous month."""
    parsed = parse_datetime("2026-07-01T00:12:00-04:00")
    assert parsed.astimezone(UTC).month == 7
    assert parsed.day == 1 and parsed.hour == 4


def test_epoch_seconds_parse():
    """Hacker News supplies created_at_i as epoch seconds."""
    parsed = parse_datetime(1757000000)
    assert parsed is not None and parsed.tzinfo is not None


def test_naive_input_is_assumed_utc():
    parsed = parse_datetime("2026-03-04 20:00:29")
    assert parsed.tzinfo is not None


def test_results_are_mutually_comparable():
    """Mixing naive and aware datetimes raises TypeError. Every source must
    normalise to the same kind or sorting the corpus by date blows up."""
    a = parse_datetime("2024-01-02T16:02:48Z")
    b = parse_datetime(1757000000)
    c = parse_datetime(datetime(2025, 6, 1))
    assert sorted([a, b, c])  # would raise if any were naive


@pytest.mark.parametrize("value", [None, "", "not a date", "yesterday-ish", {}, []])
def test_unparseable_returns_none_not_raises(value):
    """One malformed date must not abort a crawl of thousands of postings."""
    assert parse_datetime(value) is None


@pytest.mark.parametrize("value", ["1970-01-01", "1999-12-31", "2049-01-01", 0])
def test_implausible_dates_are_rejected(value):
    """Parse artefacts create phantom year cells that look like real data."""
    assert parse_datetime(value) is None


def test_plausible_boundary_years_are_kept():
    assert parse_datetime("2015-06-01") is not None
    assert parse_datetime("2030-06-01") is not None


def test_year_month_shape():
    assert year_month(parse_datetime("2024-01-02T16:02:48Z")) == (2024, "2024-01")
    assert year_month(None) == (None, None)


def test_year_month_pads_single_digit_months():
    """'2024-3' would sort after '2024-12' as a string and scramble the series."""
    assert year_month(parse_datetime("2024-03-04"))[1] == "2024-03"
