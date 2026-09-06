"""Canonical SQLite store.

Two responsibilities:

* ``jobs`` — every posting collected, holding the full description so skill
  extraction can be re-run against a changed taxonomy at zero network cost. This
  is what makes the taxonomy cheap to evolve.
* ``crawl_queue`` — per-URL state, so a long crawl is resumable. Kill the process
  at 60% and the next run picks up where it stopped rather than starting over.

``failed_fetches`` records what went wrong instead of swallowing it. The previous
scraper's ``except: append(0)`` silently turned failures into zeroes, which is
indistinguishable from a skill genuinely not appearing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

from .models import RawJob

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id           TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    source_group     TEXT NOT NULL,
    native_id        TEXT NOT NULL,
    url              TEXT NOT NULL,
    title            TEXT NOT NULL,
    description_html TEXT,
    company          TEXT,
    location         TEXT,
    country          TEXT,
    is_remote        INTEGER,
    date_posted      TEXT,
    year             INTEGER,
    month            TEXT,
    valid_through    TEXT,
    employment_type  TEXT,
    salary_min       REAL,
    salary_max       REAL,
    salary_currency  TEXT,
    industry         TEXT,
    department       TEXT,
    fetched_at       TEXT,
    is_historical    INTEGER NOT NULL DEFAULT 0,
    extra            TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_source      ON jobs (source);
CREATE INDEX IF NOT EXISTS idx_jobs_group_year  ON jobs (source_group, year);
CREATE INDEX IF NOT EXISTS idx_jobs_month       ON jobs (month);

CREATE TABLE IF NOT EXISTS crawl_queue (
    url         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'detail',
    state       TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    queued_at   TEXT NOT NULL,
    updated_at  TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_state ON crawl_queue (source, state);

CREATE TABLE IF NOT EXISTS failed_fetches (
    url         TEXT NOT NULL,
    source      TEXT NOT NULL,
    status_code INTEGER,
    reason      TEXT,
    failed_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_log (
    run_id     TEXT NOT NULL,
    stage      TEXT NOT NULL,
    source     TEXT,
    stats      TEXT,
    started_at TEXT,
    ended_at   TEXT
);
"""


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


class JobStore:
    """SQLite-backed canonical store."""

    def __init__(self, path: str | Path = "data/raw/jobs.sqlite") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---------------------------------------------------------------- jobs

    def upsert_job(self, job: RawJob) -> None:
        self.upsert_jobs([job])

    def upsert_jobs(self, jobs: Iterable[RawJob]) -> int:
        rows = []
        for job in jobs:
            data = asdict(job)
            data["job_id"] = job.job_id
            data["year"] = job.year
            data["month"] = job.month
            data["date_posted"] = _iso(job.date_posted)
            data["valid_through"] = _iso(job.valid_through)
            data["fetched_at"] = _iso(job.fetched_at)
            data["is_remote"] = None if job.is_remote is None else int(job.is_remote)
            data["is_historical"] = int(job.is_historical)
            data["extra"] = json.dumps(job.extra) if job.extra else None
            rows.append(data)

        if not rows:
            return 0

        columns = [
            "job_id",
            "source",
            "source_group",
            "native_id",
            "url",
            "title",
            "description_html",
            "company",
            "location",
            "country",
            "is_remote",
            "date_posted",
            "year",
            "month",
            "valid_through",
            "employment_type",
            "salary_min",
            "salary_max",
            "salary_currency",
            "industry",
            "department",
            "fetched_at",
            "is_historical",
            "extra",
        ]
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT OR REPLACE INTO jobs ({', '.join(columns)}) VALUES ({placeholders})"
        with self._lock:
            self._conn.executemany(sql, [[row[c] for c in columns] for row in rows])
            self._conn.commit()
        return len(rows)

    def job_count(self, source: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM jobs"
        params: tuple = ()
        if source:
            sql += " WHERE source = ?"
            params = (source,)
        with self._lock:
            return self._conn.execute(sql, params).fetchone()[0]

    def counts_by_source(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, COUNT(*) FROM jobs GROUP BY source ORDER BY 2 DESC"
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def counts_by_group_year(self) -> list[tuple[str, int, int]]:
        """(source_group, year, n) — the shape the thin-cell suppression rule needs."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_group, year, COUNT(*) FROM jobs "
                "WHERE year IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2"
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def counts_by_source_group_year(
        self, history_sources: Iterable[str]
    ) -> list[tuple[str, int, int]]:
        """(source_group, year, n) counting only sources trusted for history.

        ATS boards are excluded here even though they carry real publish dates:
        their older postings are survivorship-biased, so counting them would
        overstate how much genuine history the corpus holds.
        """
        sources = list(history_sources)
        if not sources:
            return []
        placeholders = ", ".join("?" for _ in sources)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT source_group, year, COUNT(*) FROM jobs "
                f"WHERE year IS NOT NULL AND source IN ({placeholders}) "
                "GROUP BY 1, 2 ORDER BY 1, 2",
                sources,
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def iter_jobs(self, source: str | None = None, batch_size: int = 500) -> Iterator[sqlite3.Row]:
        sql = "SELECT * FROM jobs"
        params: tuple = ()
        if source:
            sql += " WHERE source = ?"
            params = (source,)
        with self._lock:
            cursor = self._conn.execute(sql, params)
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                yield from rows

    def has_job(self, source: str, native_id: str) -> bool:
        import hashlib

        job_id = hashlib.sha1(f"{source}:{native_id}".encode()).hexdigest()[:16]
        with self._lock:
            return (
                self._conn.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
                is not None
            )

    # --------------------------------------------------------- crawl queue

    def enqueue(self, urls: Iterable[str], source: str, kind: str = "detail") -> int:
        now = datetime.now(UTC).isoformat()
        rows = [(url, source, kind, "pending", 0, now) for url in urls]
        if not rows:
            return 0
        with self._lock:
            cursor = self._conn.executemany(
                "INSERT OR IGNORE INTO crawl_queue "
                "(url, source, kind, state, attempts, queued_at) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
            return cursor.rowcount

    def next_pending(self, source: str, limit: int = 100) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT url FROM crawl_queue WHERE source = ? AND state = 'pending' LIMIT ?",
                (source, limit),
            ).fetchall()
        return [row[0] for row in rows]

    def mark(self, url: str, state: str, note: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE crawl_queue SET state = ?, note = ?, updated_at = ?, "
                "attempts = attempts + 1 WHERE url = ?",
                (state, note, datetime.now(UTC).isoformat(), url),
            )
            self._conn.commit()

    def queue_stats(self, source: str | None = None) -> dict[str, int]:
        sql = "SELECT state, COUNT(*) FROM crawl_queue"
        params: tuple = ()
        if source:
            sql += " WHERE source = ?"
            params = (source,)
        sql += " GROUP BY state"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return {row[0]: row[1] for row in rows}

    # ------------------------------------------------------------ failures

    def record_failure(self, url: str, source: str, status_code: int | None, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO failed_fetches (url, source, status_code, reason, failed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (url, source, status_code, reason, datetime.now(UTC).isoformat()),
            )
            self._conn.commit()

    def failure_count(self, source: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM failed_fetches"
        params: tuple = ()
        if source:
            sql += " WHERE source = ?"
            params = (source,)
        with self._lock:
            return self._conn.execute(sql, params).fetchone()[0]

    # ---------------------------------------------------------------- misc

    def log_run(
        self, run_id: str, stage: str, source: str | None, stats: dict, started_at, ended_at
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO run_log (run_id, stage, source, stats, started_at, ended_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, stage, source, json.dumps(stats), _iso(started_at), _iso(ended_at)),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
