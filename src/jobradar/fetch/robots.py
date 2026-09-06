"""robots.txt enforcement.

This is a hard gate, not a convention. ``RobotsGate.check`` raises rather than
returning a warning, so a disallowed URL cannot be fetched by accident.

BrighterMonday makes this a live constraint rather than a formality: it disallows
every search query string (``?q=``, ``?page=``, ``?keywords=``) while explicitly
allowing ``/listings/*`` detail pages and ``page=2`` through ``page=7``.

Why Protego and not ``urllib.robotparser``
------------------------------------------
The stdlib parser is not conformant with RFC 9309 on blank lines. It treats a
blank line as a group terminator, so given the real BrighterMonday file::

    User-agent: *
                        <- blank line
    Disallow: /api/

it discards the whole group and reports *everything* as allowed. That is a silent
compliance failure — the crawl would look polite while ignoring every rule. The
stdlib is also inconsistent on ``*`` and ``$`` wildcards, which these sites rely
on heavily. Protego (the parser Scrapy uses) handles both correctly.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from protego import Protego


class RobotsDisallowed(RuntimeError):
    """Raised when a URL is disallowed by the host's robots.txt."""

    def __init__(self, url: str, user_agent: str) -> None:
        super().__init__(f"robots.txt disallows {url!r} for user-agent {user_agent!r}")
        self.url = url
        self.user_agent = user_agent


@dataclass
class _CachedRules:
    parser: Protego | None
    fetched_at: float
    # True when robots.txt could not be retrieved at all. We fail open in that
    # case (a 404 conventionally means "no restrictions"), but we record it so
    # the distinction stays visible rather than looking like a permissive file.
    unavailable: bool


class RobotsGate:
    """Fetches, caches and applies robots.txt per domain."""

    def __init__(
        self,
        user_agent: str,
        *,
        session=None,
        ttl_seconds: float = 24 * 3600,
        timeout: float = 20.0,
    ) -> None:
        self.user_agent = user_agent
        self._session = session
        self._ttl = ttl_seconds
        self._timeout = timeout
        self._cache: dict[str, _CachedRules] = {}
        self._lock = threading.Lock()

    def _session_or_default(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _load(self, scheme: str, netloc: str) -> _CachedRules:
        robots_url = f"{scheme}://{netloc}/robots.txt"
        try:
            response = self._session_or_default().get(
                robots_url,
                timeout=self._timeout,
                headers={"User-Agent": self.user_agent},
            )
        except Exception:
            return _CachedRules(parser=None, fetched_at=time.time(), unavailable=True)

        if response.status_code >= 400:
            # 404 means no rules; 5xx means we could not ask. Both fail open, but
            # only a 5xx is genuinely "unavailable".
            return _CachedRules(
                parser=None, fetched_at=time.time(), unavailable=response.status_code >= 500
            )

        try:
            parser = Protego.parse(response.text)
        except Exception:
            return _CachedRules(parser=None, fetched_at=time.time(), unavailable=True)

        return _CachedRules(parser=parser, fetched_at=time.time(), unavailable=False)

    def _rules_for(self, url: str) -> _CachedRules:
        parts = urlsplit(url)
        key = parts.netloc
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and (time.time() - cached.fetched_at) < self._ttl:
                return cached
        rules = self._load(parts.scheme or "https", parts.netloc)
        with self._lock:
            self._cache[key] = rules
        return rules

    def is_allowed(self, url: str) -> bool:
        rules = self._rules_for(url)
        if rules.parser is None:
            return True
        return bool(rules.parser.can_fetch(url, self.user_agent))

    def check(self, url: str) -> None:
        """Raise :class:`RobotsDisallowed` if ``url`` may not be fetched."""
        if not self.is_allowed(url):
            raise RobotsDisallowed(url, self.user_agent)

    def crawl_delay(self, url: str) -> float | None:
        """Host-declared crawl delay, if any.

        Callers take the stricter of this and their configured rate — a host
        asking for more space than our default gets it.
        """
        rules = self._rules_for(url)
        if rules.parser is None:
            return None
        try:
            delay = rules.parser.crawl_delay(self.user_agent)
        except Exception:
            return None
        return float(delay) if delay is not None else None

    def sitemaps(self, url: str) -> list[str]:
        """Sitemap URLs declared in robots.txt.

        Fuzu and MyJobMag both advertise job sitemaps this way, which is the
        cheapest and most robots-friendly way to enumerate their postings.
        """
        rules = self._rules_for(url)
        if rules.parser is None:
            return []
        try:
            return list(rules.parser.sitemaps)
        except Exception:
            return []
