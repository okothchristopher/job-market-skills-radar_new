"""Wiring adapters to the fetch core and the store.

Discovery and fetching are separate passes on purpose. Discovery fills the crawl
queue and is cheap; fetching drains it and is the expensive part. Because queue
state lives in SQLite, a fetch killed at 60% resumes rather than restarting.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime

import yaml

from .config import REPO_ROOT, Config
from .fetch.client import FetchError, PoliteClient, build_client
from .fetch.robots import RobotsDisallowed
from .sources import registry
from .sources.ashby import AshbyAdapter
from .sources.greenhouse import GreenhouseAdapter
from .store.db import JobStore

log = logging.getLogger(__name__)

# Group aliases the CLI accepts, so `--sources global` works as well as a list.
GROUP_ALIASES = {"ke": "KE", "kenya": "KE", "global": "GLOBAL", "region": "REGION"}

# Runaway guard for the drain loop. Adapters that enqueue follow-on pages could in
# principle do so forever; this bounds the damage without capping legitimate
# pagination, which is separately limited per adapter.
MAX_DRAIN_ROUNDS = 200


def load_companies(path: str = "config/companies.yaml") -> list[dict]:
    """Load confirmed ATS boards. Returns [] if the bootstrap has not run yet."""
    p = REPO_ROOT / path
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data.get("companies") or []


def build_adapter(name: str, client: PoliteClient, config: Config):
    """Instantiate one adapter with its config, injecting companies where needed."""
    adapter_cls = registry.get(name)
    try:
        source_config = config.source(name)
    except KeyError:
        source_config = {}

    if adapter_cls in (GreenhouseAdapter, AshbyAdapter):
        companies = load_companies(source_config.get("companies_file", "config/companies.yaml"))
        return adapter_cls(client, source_config, companies=companies)
    return adapter_cls(client, source_config)


def resolve_sources(spec: str, config: Config) -> list[str]:
    """Turn ``"global"`` or ``"greenhouse,ashby"`` into concrete adapter names."""
    spec = (spec or "").strip()
    if not spec:
        return []

    group = GROUP_ALIASES.get(spec.lower())
    if group:
        # Only sources that both have an adapter and are enabled in config.
        return [n for n in registry.names(group) if n in set(config.source_names(group=group))]

    names = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part not in registry:
            raise KeyError(f"unknown source {part!r}; known: {sorted(registry.names())}")
        names.append(part)
    return names


def client_for(config: Config) -> PoliteClient:
    return build_client(
        cache_path=str(config.path("http_cache")),
        cache_ttl_seconds=config.cache_ttl_seconds,
        default_rate=config.defaults.get("rate_per_second", 0.5),
        domain_rates=config.domain_rates(),
        user_agent=config.user_agent,
    )


def discover(source_names: Iterable[str], config: Config, store: JobStore) -> dict[str, int]:
    """Populate the crawl queue for each source. Returns URLs queued per source."""
    client = client_for(config)
    queued: dict[str, int] = {}

    for name in source_names:
        adapter = build_adapter(name, client, config)
        try:
            urls = list(adapter.discover())
        except (FetchError, RobotsDisallowed) as exc:
            log.warning("discovery failed for %s: %s", name, exc)
            queued[name] = 0
            continue
        queued[name] = store.enqueue(urls, source=name)
        log.info("%s: discovered %d URLs (%d new)", name, len(urls), queued[name])

    return queued


def fetch(
    source_names: Iterable[str],
    config: Config,
    store: JobStore,
    limit: int = 0,
    client: PoliteClient | None = None,
) -> dict[str, dict]:
    """Drain the queue for each source, parsing and storing as we go.

    A single bad URL must never abort a long crawl, so failures are recorded in
    ``failed_fetches`` and the loop continues. That table is the reason a run's
    gaps stay visible instead of silently becoming zeroes.
    """
    client = client or client_for(config)
    results: dict[str, dict] = {}

    for name in source_names:
        adapter = build_adapter(name, client, config)
        started = datetime.now(UTC)
        stored = failed = 0

        # Adapters may enqueue follow-on pages while we drain (cursor pagination,
        # sitemap fan-out), so keep going until the queue is empty rather than
        # snapshotting it once. The cap is a runaway guard, not a target.
        processed = 0
        budget = limit or 100_000
        for _ in range(MAX_DRAIN_ROUNDS):
            pending = store.next_pending(name, limit=budget - processed)
            if not pending:
                break

            for url in pending:
                if processed >= budget:
                    break
                processed += 1

                try:
                    response = client.get(url)
                except (FetchError, RobotsDisallowed) as exc:
                    status = getattr(exc, "status_code", None)
                    store.record_failure(url, name, status, str(exc)[:300])
                    store.mark(url, "failed", str(exc)[:200])
                    failed += 1
                    continue

                try:
                    jobs = list(adapter.parse(response))
                    follow = list(adapter.next_urls(response))
                except Exception as exc:  # a parser bug on one page, not a crawl-ender
                    log.exception("parse failed for %s", url)
                    store.record_failure(url, name, None, f"parse: {exc}"[:300])
                    store.mark(url, "failed", "parse error")
                    failed += 1
                    continue

                for job in jobs:
                    if job.fetched_at is None:
                        job.fetched_at = datetime.now(UTC)
                stored += store.upsert_jobs(jobs)
                store.mark(url, "done", f"{len(jobs)} jobs")

                if follow:
                    store.enqueue(follow, source=name)

            if processed >= budget:
                break

        stats = {
            "urls": processed,
            "jobs_stored": stored,
            "failed": failed,
            "client": client.stats.as_dict(),
        }
        results[name] = stats
        store.log_run(
            run_id=started.strftime("%Y%m%dT%H%M%S"),
            stage="fetch",
            source=name,
            stats=stats,
            started_at=started,
            ended_at=datetime.now(UTC),
        )
        log.info("%s: %d URLs -> %d jobs (%d failed)", name, processed, stored, failed)

    return results
