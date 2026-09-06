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

CREATE TABLE IF NOT EXISTS job_skills (
    job_id         TEXT NOT NULL,
    skill          TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    category       TEXT,
    zindua_track   TEXT,
    n_mentions     INTEGER NOT NULL,
    matched_in     TEXT NOT NULL,
    PRIMARY KEY (job_id, skill)
);
CREATE INDEX IF NOT EXISTS idx_job_skills_skill ON job_skills (skill);
CREATE INDEX IF NOT EXISTS idx_job_skills_track ON job_skills (zindua_track);

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
        # WAL allows concurrent readers, but still only one writer. Without a
        # busy timeout a second writer fails instantly with "database is
        # locked" -- which is how a crawl died mid-run when extraction was
        # working on the same file. Wait for the lock instead of aborting.
        self._conn.execute("PRAGMA busy_timeout=30000")
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

        # Replayed sources are stored per board as "wayback_brightermonday", so
        # the configured entry "wayback" has to match by prefix. Exact matching
        # made `status` report no Kenyan history at all while the analysis layer
        # -- which does match by prefix -- was already using thousands of
        # backfilled postings. Two views of the same corpus disagreeing is worse
        # than either being wrong alone.
        clauses = []
        params: list = []
        for name in sources:
            clauses.append("source = ?")
            params.append(name)
            clauses.append(r"source LIKE ? ESCAPE '\'")
            params.append(rf"{name}\_%")

        with self._lock:
            rows = self._conn.execute(
                "SELECT source_group, year, COUNT(*) FROM jobs "
                f"WHERE year IS NOT NULL AND ({' OR '.join(clauses)}) "
                "GROUP BY 1, 2 ORDER BY 1, 2",
                params,
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def iter_jobs(self, source: str | None = None, batch_size: int = 500) -> Iterator[sqlite3.Row]:
        """Iterate stored postings in stable order.

        The lock is **released between batches**, never held across a ``yield``.
        Holding it while yielding deadlocks any caller that writes back as it
        reads — which is exactly what skill extraction does, and it hangs
        silently rather than erroring because ``threading.Lock`` is not
        reentrant.

        Paging is by ``job_id`` rather than OFFSET so the walk stays correct
        even if rows are written while it runs.
        """
        base = "SELECT * FROM jobs"
        where = []
        params: list = []
        if source:
            where.append("source = ?")
            params.append(source)

        last_id = ""
        while True:
            clauses = [*where, "job_id > ?"]
            sql = f"{base} WHERE {' AND '.join(clauses)} ORDER BY job_id LIMIT ?"
            with self._lock:
                rows = self._conn.execute(sql, (*params, last_id, batch_size)).fetchall()
            if not rows:
                break
            yield from rows
            last_id = rows[-1]["job_id"]

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

    def requeue_failed(self, source: str | None = None, max_attempts: int = 6) -> int:
        """Return failed URLs to the pending queue.

        Transient network faults are not permanent facts about a URL. A DNS
        blip mid-crawl marked 624 Fuzu postings failed; every one of them
        resolved fine minutes later. Without this they would be lost until
        someone noticed the count was short.

        ``max_attempts`` stops a genuinely dead URL from being retried forever.
        """
        sql = "UPDATE crawl_queue SET state = 'pending' WHERE state = 'failed' AND attempts < ?"
        params: list = [max_attempts]
        if source:
            sql += " AND source = ?"
            params.append(source)
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor.rowcount

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

    # -------------------------------------------------------------- skills

    def replace_job_skills(self, job_id: str, matches: Iterable) -> int:
        """Write one job's skill matches, replacing any previous extraction.

        Replacing rather than appending is what makes re-running extraction
        after a taxonomy fix safe: the corrected result overwrites the old one
        instead of accumulating both.
        """
        rows = [
            (
                job_id,
                m.skill,
                m.canonical_name,
                m.category,
                m.zindua_track,
                m.n_mentions,
                m.matched_in,
            )
            for m in matches
        ]
        with self._lock:
            self._conn.execute("DELETE FROM job_skills WHERE job_id = ?", (job_id,))
            if rows:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO job_skills "
                    "(job_id, skill, canonical_name, category, zindua_track, "
                    "n_mentions, matched_in) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
        return len(rows)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def clear_job_skills(self) -> int:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM job_skills")
            self._conn.commit()
            return cursor.rowcount

    def skill_match_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM job_skills").fetchone()[0]

    def jobs_with_skills_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(DISTINCT job_id) FROM job_skills").fetchone()[0]

    def top_skills(self, source_group: str | None = None, limit: int = 20):
        """(skill, track, n_jobs) ordered by how many postings mention them."""
        sql = (
            "SELECT s.skill, s.zindua_track, COUNT(DISTINCT s.job_id) n "
            "FROM job_skills s JOIN jobs j ON j.job_id = s.job_id "
        )
        params: tuple = ()
        if source_group:
            sql += "WHERE j.source_group = ? "
            params = (source_group,)
        sql += "GROUP BY s.skill, s.zindua_track ORDER BY n DESC LIMIT ?"
        with self._lock:
            return self._conn.execute(sql, (*params, limit)).fetchall()

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
