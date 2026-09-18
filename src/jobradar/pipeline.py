"""Wiring adapters to the fetch core and the store.

Discovery and fetching are separate passes on purpose. Discovery fills the crawl
queue and is cheap; fetching drains it and is the expensive part. Because queue
state lives in SQLite, a fetch killed at 60% resumes rather than restarting.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .config import REPO_ROOT, Config
from .extract.skills import SkillExtractor
from .fetch.client import FetchError, PoliteClient, build_client
from .fetch.robots import RobotsDisallowed
from .parse.text import html_to_text
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


def extract_skills(
    config: Config,
    store: JobStore,
    taxonomy_path: str | None = None,
    batch_size: int = 500,
) -> dict:
    """Run the taxonomy over every stored posting.

    This costs no network traffic at all -- descriptions are already in the
    store, which is the whole reason for collecting documents rather than search
    counts. Re-running after a taxonomy correction is free, so the taxonomy can
    keep improving without ever re-crawling.
    """
    extractor = SkillExtractor.from_csv(taxonomy_path or config.path("taxonomy"))

    jobs = matches = with_skills = 0
    for row in store.iter_jobs(batch_size=batch_size):
        jobs += 1
        text = html_to_text(row["description_html"])
        found = extractor.extract(row["title"], text)
        n = store.replace_job_skills(row["job_id"], found)
        matches += n
        if n:
            with_skills += 1
        if jobs % batch_size == 0:
            store.commit()
            log.info("extracted %d/%s jobs", jobs, "?")
    store.commit()

    return {
        "skills_in_taxonomy": len(extractor.skills),
        "jobs_processed": jobs,
        "jobs_with_skills": with_skills,
        "skill_matches": matches,
        "coverage": round(with_skills / jobs, 4) if jobs else 0.0,
    }


def aggregate(config: Config, out_dir: str | None = None) -> tuple[dict, dict]:
    """Build every tidy frame and the diffusion table, and write them to disk.

    Returns ``(written_paths, diagnostics)``. Diagnostics carry the calibration
    offset and the data-sufficiency flags, so a caller can see what the numbers
    rest on rather than only the numbers.
    """
    from .aggregate import diffusion as dif
    from .aggregate import frames as fr

    db = config.path("raw_db")
    jobs = fr.load_jobs(db)
    job_skills = fr.load_job_skills(db, jobs=jobs)

    skill_year = fr.build_skill_year(config, jobs, job_skills)
    skill_month = fr.build_skill_month(config, jobs, job_skills)
    skill_current = fr.build_skill_current(config, jobs, job_skills)

    kenya_months = int(jobs.loc[jobs["source_group"] == "KE", "month"].nunique())
    settings = dif.DiffusionSettings.from_config(config)
    skill_diffusion, diagnostics = dif.build_diffusion(
        skill_current,
        skill_month,
        settings,
        kenya_months_observed=kenya_months,
        skill_year=skill_year,
    )
    # Test the trickle-down thesis rather than assuming it (PLAN.md section 11a).
    diagnostics["backtest"] = dif.backtest_thesis(skill_year)

    frames = {
        "jobs": jobs,
        "skill_current": skill_current,
        "skill_year": skill_year,
        "skill_month": skill_month,
        "skill_diffusion": skill_diffusion,
        "watchlist": dif.watchlist(skill_diffusion, limit=40),
        "track_summary": dif.track_summary(skill_diffusion),
    }
    written = fr.export(frames, out_dir or config.path("processed"))
    return written, diagnostics


def build_reports(config: Config, out_dir: str | None = None) -> dict:
    """Generate the curriculum briefs from the processed frames."""
    from .aggregate import diffusion as dif
    from .aggregate import frames as fr
    from .report import track_brief as tb

    db = config.path("raw_db")
    jobs = fr.load_jobs(db)
    job_skills = fr.load_job_skills(db, jobs=jobs)

    skill_year = fr.build_skill_year(config, jobs, job_skills)
    skill_month = fr.build_skill_month(config, jobs, job_skills)
    skill_current = fr.build_skill_current(config, jobs, job_skills)

    settings = dif.DiffusionSettings.from_config(config)
    kenya_months = int(jobs.loc[jobs["source_group"] == "KE", "month"].nunique())
    skill_diffusion, diagnostics = dif.build_diffusion(
        skill_current,
        skill_month,
        settings,
        kenya_months_observed=kenya_months,
        skill_year=skill_year,
    )
    diagnostics["total_postings"] = len(jobs)
    backtest = dif.backtest_thesis(skill_year)

    coverage = tb.track_coverage(job_skills, jobs)
    briefs = tb.build_briefs(skill_diffusion, coverage)

    out = Path(out_dir or config.path("reports"))
    out.mkdir(parents=True, exist_ok=True)
    markdown = tb.to_markdown(briefs, backtest, diagnostics)
    (out / "curriculum_briefs.md").write_text(markdown, encoding="utf-8")
    coverage.to_csv(out / "track_coverage.csv", index=False)

    return {
        "briefs": briefs,
        "backtest": backtest,
        "diagnostics": diagnostics,
        "coverage": coverage,
        "markdown_path": str(out / "curriculum_briefs.md"),
    }
