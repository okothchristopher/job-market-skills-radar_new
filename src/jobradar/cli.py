"""Command-line entry point.

Stages are separate commands so each reruns independently. Re-running ``extract``
after editing the taxonomy costs no network traffic at all, because descriptions
are already in the store — that is the whole point of collecting documents rather
than counts.
"""

from __future__ import annotations

import logging

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from . import pipeline
from .config import Config
from .fetch.client import build_client
from .store.db import JobStore

app = typer.Typer(
    add_completion=False,
    help="Job market skills radar — global and Kenyan tech skill demand.",
)
console = Console()


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


def _client(config: Config):
    return build_client(
        cache_path=str(config.path("http_cache")),
        cache_ttl_seconds=config.cache_ttl_seconds,
        default_rate=config.defaults.get("rate_per_second", 0.5),
        domain_rates=config.domain_rates(),
        user_agent=config.user_agent,
    )


@app.command()
def sources(group: str = typer.Option(None, help="Filter by group: KE, GLOBAL, REGION")) -> None:
    """List configured sources and their crawl policy."""
    config = Config.load()
    table = Table(title="Configured sources")
    for column in ("name", "group", "domain", "req/s", "enabled"):
        table.add_column(column)

    for name in config.source_names(group=group, enabled_only=False):
        entry = config.source(name)
        table.add_row(
            name,
            str(entry.get("group") or "-"),
            str(entry.get("domain") or "-"),
            str(entry.get("rate_per_second")),
            "yes" if entry.get("enabled", True) else "no",
        )
    console.print(table)


@app.command()
def robots(url: str) -> None:
    """Check whether a URL may be fetched, and why.

    Useful when planning a crawl: BrighterMonday disallows every search query
    string but allows /listings/ detail pages, and that is easy to get wrong.
    """
    _setup_logging()
    config = Config.load()
    client = _client(config)
    allowed = client.robots.is_allowed(url)
    delay = client.robots.crawl_delay(url)
    console.print(f"[bold]{url}[/bold]")
    console.print(f"  allowed: {'[green]yes[/green]' if allowed else '[red]no[/red]'}")
    console.print(f"  crawl-delay: {delay if delay is not None else 'not specified'}")
    console.print(f"  user-agent: {client.user_agent}")


@app.command()
def status() -> None:
    """Show what has been collected so far."""
    config = Config.load()
    store = JobStore(config.path("raw_db"))

    total = store.job_count()
    console.print(f"\n[bold]{total:,}[/bold] postings in the store\n")

    by_source = store.counts_by_source()
    if by_source:
        table = Table(title="By source")
        table.add_column("source")
        table.add_column("postings", justify="right")
        for name, count in by_source.items():
            table.add_row(name, f"{count:,}")
        console.print(table)

    rows = store.counts_by_group_year()
    if rows:
        table = Table(title="By group and year")
        for column in ("group", "year", "postings", "usable?"):
            table.add_column(column)
        threshold = config.min_cell_size
        for group, year, count in rows:
            usable = (
                "[green]yes[/green]"
                if count >= threshold
                else f"[yellow]thin (<{threshold})[/yellow]"
            )
            table.add_row(group, str(year), f"{count:,}", usable)
        console.print(table)
        console.print(
            f"\n[dim]Cells below {threshold} postings are excluded from trend claims.[/dim]"
        )

    _print_trend_usability(config, store)

    failures = store.failure_count()
    if failures:
        console.print(f"\n[yellow]{failures:,} failed fetches recorded[/yellow]")
    store.close()


def _print_trend_usability(config: Config, store: JobStore) -> None:
    """Show how much of the corpus may actually carry a trend claim.

    Two filters apply, and both remove data that looks perfectly good:

    * ATS boards (Greenhouse, Ashby) carry real publish dates but their older
      postings are survivorship-biased — a 2024 role still open today is
      evergreen or hard to fill, not a sample of 2024 demand.
    * Any (group, year) cell below ``min_cell_size`` is too thin to support a
      claim regardless of source.

    Printing this next to the raw totals keeps the gap between "collected" and
    "usable" visible, which is where over-claiming usually starts.
    """
    analysis = config.settings.get("analysis", {})
    unbiased = set(analysis.get("unbiased_history_sources", []))
    if not unbiased:
        return

    rows = store.counts_by_source_group_year(history_sources=unbiased)
    if not rows:
        return

    table = Table(title="Trend-usable history (survivorship filter applied)")
    for column in ("group", "year", "unbiased postings", "usable?"):
        table.add_column(column)

    threshold = config.min_cell_size
    for group, year, count in rows:
        usable = (
            "[green]yes[/green]" if count >= threshold else f"[yellow]thin (<{threshold})[/yellow]"
        )
        table.add_row(group, str(year), f"{count:,}", usable)
    console.print(table)
    console.print(
        f"[dim]Only {', '.join(sorted(unbiased))} carry year-over-year trends. "
        "ATS boards supply current-state depth; their older tail is excluded.[/dim]"
    )


@app.command()
def cache(purge: bool = typer.Option(False, help="Delete entries past their TTL")) -> None:
    """Inspect or purge the HTTP response cache."""
    config = Config.load()
    from .fetch.cache import ResponseCache

    response_cache = ResponseCache(config.path("http_cache"), ttl_seconds=config.cache_ttl_seconds)
    if purge:
        removed = response_cache.purge_expired()
        console.print(f"purged {removed:,} expired entries")
    stats = response_cache.stats()
    console.print(
        f"{stats['entries']:,} cached responses, "
        f"{stats['compressed_bytes'] / 1e6:.1f} MB compressed"
    )
    response_cache.close()


@app.command()
def discover(
    sources: str = typer.Option(..., help="Comma-separated source names, or a group: ke, global"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Populate the crawl queue for the given sources."""
    _setup_logging(verbose)
    config = Config.load()
    store = JobStore(config.path("raw_db"))
    try:
        names = pipeline.resolve_sources(sources, config)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    if not names:
        console.print(f"[yellow]no sources matched {sources!r}[/yellow]")
        raise typer.Exit(code=1)

    queued = pipeline.discover(names, config, store)

    table = Table(title="Discovered")
    table.add_column("source")
    table.add_column("new URLs queued", justify="right")
    for name, count in queued.items():
        table.add_row(name, f"{count:,}")
    console.print(table)
    store.close()


@app.command()
def fetch(
    sources: str = typer.Option(..., help="Comma-separated source names, or a group"),
    limit: int = typer.Option(0, help="Stop after this many URLs per source (0 = no limit)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Drain the crawl queue, parsing and storing postings."""
    _setup_logging(verbose)
    config = Config.load()
    store = JobStore(config.path("raw_db"))
    try:
        names = pipeline.resolve_sources(sources, config)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    if not names:
        console.print(f"[yellow]no sources matched {sources!r}[/yellow]")
        raise typer.Exit(code=1)

    results = pipeline.fetch(names, config, store, limit=limit)

    table = Table(title="Fetched")
    for column in ("source", "URLs", "jobs stored", "failed"):
        table.add_column(column, justify="right" if column != "source" else "left")
    for name, stats in results.items():
        table.add_row(
            name, f"{stats['urls']:,}", f"{stats['jobs_stored']:,}", f"{stats['failed']:,}"
        )
    console.print(table)
    console.print(f"\n[dim]{store.job_count():,} postings in the store.[/dim]")
    store.close()


@app.command()
def extract(
    taxonomy: str = typer.Option(None, help="Override the taxonomy CSV path"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the skill taxonomy over every stored posting.

    Costs no network traffic: descriptions are already in the store, so a
    taxonomy correction can be re-applied for free.
    """
    _setup_logging(verbose)
    config = Config.load()
    store = JobStore(config.path("raw_db"))

    stats = pipeline.extract_skills(config, store, taxonomy_path=taxonomy)

    console.print(
        f"\n[bold]{stats['skill_matches']:,}[/bold] skill matches across "
        f"[bold]{stats['jobs_with_skills']:,}[/bold] of {stats['jobs_processed']:,} postings "
        f"({stats['coverage']:.1%} coverage), using {stats['skills_in_taxonomy']} skills\n"
    )

    for group in ("GLOBAL", "KE"):
        rows = store.top_skills(source_group=group, limit=12)
        if not rows:
            continue
        table = Table(title=f"Most-requested skills — {group}")
        for column in ("skill", "Zindua track", "postings"):
            table.add_column(column, justify="right" if column == "postings" else "left")
        for skill, track, n in rows:
            table.add_row(skill, track or "-", f"{n:,}")
        console.print(table)
    store.close()


if __name__ == "__main__":
    app()
