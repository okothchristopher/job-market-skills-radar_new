"""The contract every source adapter implements.

Two methods. ``discover`` yields URLs (or API page descriptors) worth fetching;
``parse`` turns one fetched response into ``RawJob`` records. Keeping discovery
and parsing separate is what makes crawls resumable: discovery fills the queue,
fetching drains it, and a killed process loses neither.

Adapters never call ``requests``. They receive a :class:`PoliteClient`, which is
where robots enforcement, rate limiting and caching live.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator

from ..fetch.client import PoliteClient, Response
from ..store.models import RawJob


class SourceAdapter(ABC):
    """Base class for all sources.

    Attributes:
        name: Adapter identifier, matching the key in ``config/sources.yaml``.
        group: One of ``KE``, ``GLOBAL``, ``REGION``.
    """

    name: str = "base"
    group: str = "GLOBAL"

    def __init__(self, client: PoliteClient, config: dict | None = None) -> None:
        self.client = client
        self.config = config or {}

    @abstractmethod
    def discover(self) -> Iterator[str]:
        """Yield URLs to fetch.

        Implementations should prefer sitemaps and category paths over search
        query strings — both BrighterMonday and MyJobMag disallow query strings
        in robots.txt, and category crawling is cheaper anyway.
        """

    @abstractmethod
    def parse(self, response: Response) -> Iterable[RawJob]:
        """Turn one fetched response into zero or more postings.

        Returning many is normal: a Greenhouse company board is one request
        carrying hundreds of postings, and a Hacker News thread is one request
        carrying hundreds of comments.
        """

    def is_detail_url(self, url: str) -> bool:
        """Whether a discovered URL is a posting rather than an index page."""
        return True

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} group={self.group!r}>"


class AdapterRegistry:
    """Maps adapter names to classes, so the CLI can resolve ``--sources ke``."""

    def __init__(self) -> None:
        self._adapters: dict[str, type[SourceAdapter]] = {}

    def register(self, adapter_cls: type[SourceAdapter]) -> type[SourceAdapter]:
        self._adapters[adapter_cls.name] = adapter_cls
        return adapter_cls

    def get(self, name: str) -> type[SourceAdapter]:
        if name not in self._adapters:
            raise KeyError(f"unknown source {name!r}; known: {sorted(self._adapters)}")
        return self._adapters[name]

    def names(self, group: str | None = None) -> list[str]:
        if group is None:
            return sorted(self._adapters)
        return sorted(
            name for name, cls in self._adapters.items() if cls.group.upper() == group.upper()
        )

    def __contains__(self, name: str) -> bool:
        return name in self._adapters


registry = AdapterRegistry()
