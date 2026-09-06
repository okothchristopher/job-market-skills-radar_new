"""Persistent HTTP response cache backed by SQLite.

This is the single biggest lever against rate limiting. Parser bugs are normal and
get fixed several times during a build; without a cache, every fix means
re-fetching thousands of pages. With it, a re-run costs zero requests until the
TTL expires.

Bodies are stored compressed. A 600KB BrighterMonday category page compresses to
roughly a tenth of that, so a full crawl's cache stays in the tens of megabytes.
"""

from __future__ import annotations

import gzip
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    key         TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    headers     TEXT NOT NULL,
    body        BLOB NOT NULL,
    encoding    TEXT,
    fetched_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_http_cache_fetched_at ON http_cache (fetched_at);
"""


@dataclass
class CachedResponse:
    url: str
    status_code: int
    headers: dict[str, str]
    body: bytes
    encoding: str | None
    fetched_at: float

    @property
    def text(self) -> str:
        return self.body.decode(self.encoding or "utf-8", errors="replace")


class ResponseCache:
    """SQLite-backed cache keyed by request URL.

    Args:
        path: Database file. Parent directories are created.
        ttl_seconds: Entries older than this are treated as misses. The default of
            14 days suits a monthly collection cadence: within one run's
            development cycle everything is cached, but the next month's run
            re-fetches rather than serving stale postings.
    """

    def __init__(self, path: str | Path, ttl_seconds: float = 14 * 24 * 3600) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        # WAL lets readers proceed while a write is in flight, which matters once
        # domains are fetched in parallel.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @staticmethod
    def _key(url: str) -> str:
        return url

    def get(self, url: str) -> CachedResponse | None:
        import json

        with self._lock:
            row = self._conn.execute(
                "SELECT url, status_code, headers, body, encoding, fetched_at "
                "FROM http_cache WHERE key = ?",
                (self._key(url),),
            ).fetchone()
        if row is None:
            return None
        if (time.time() - row[5]) > self.ttl:
            return None
        return CachedResponse(
            url=row[0],
            status_code=row[1],
            headers=json.loads(row[2]),
            body=gzip.decompress(row[3]),
            encoding=row[4],
            fetched_at=row[5],
        )

    def put(
        self,
        url: str,
        status_code: int,
        headers: dict[str, str],
        body: bytes,
        encoding: str | None = None,
    ) -> None:
        import json

        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO http_cache "
                "(key, url, status_code, headers, body, encoding, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self._key(url),
                    url,
                    status_code,
                    json.dumps(dict(headers)),
                    gzip.compress(body),
                    encoding,
                    time.time(),
                ),
            )
            self._conn.commit()

    def purge_expired(self) -> int:
        cutoff = time.time() - self.ttl
        with self._lock:
            cursor = self._conn.execute("DELETE FROM http_cache WHERE fetched_at < ?", (cutoff,))
            self._conn.commit()
            return cursor.rowcount

    def stats(self) -> dict[str, int]:
        with self._lock:
            total, bytes_stored = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(body)), 0) FROM http_cache"
            ).fetchone()
        return {"entries": total, "compressed_bytes": bytes_stored}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
