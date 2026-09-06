# Job Market Skills Radar

Measures which skills, languages and frameworks employers actually ask for — globally and in Kenya — and uses the **global signal as a leading indicator** for what [Zindua School](https://zinduaschool.com) should teach before the local market asks for it.

See [PLAN.md](PLAN.md) for the full design, the evidence behind each source choice, and the honest limits of the data.

## The thesis

Skills that rise in global demand trickle down to the Kenyan market after some lag. If that lag is real and measurable, Zindua can teach ahead of local demand instead of reacting to it.

It is treated as a **falsifiable hypothesis, not an assumption**. The diffusion engine classifies every skill, and the `global_only` status exists specifically to catch global trends that never arrive — scale-dependent tooling, US-specific compliance stacks, technology tied to company sizes Kenya does not yet have. Distinguishing those from genuine `teach_ahead` candidates is the most valuable thing this project produces.

## How it works

Rather than asking a job board *"how many results for `"web development" "python"`?"* and reading the count, this collects **job documents once** and extracts skills offline.

That inversion is what makes everything else possible: counts carry no date, so they cannot support a trend; documents do. It also means changing the skill taxonomy costs zero network requests — add `LangGraph` next month and re-run extraction over text already in the store.

```
FETCH ──▶ SOURCE ADAPTERS ──▶ STORE ──▶ EXTRACT ──▶ AGGREGATE ──▶ REPORT
 robots      KE + global      SQLite     skills     diffusion    watchlist
 ratelimit   + wayback        canonical  taxonomy   engine       track briefs
 cache
```

## Sources

**Kenya** — BrighterMonday, MyJobMag, Fuzu, JobWebKenya. All serve server-side HTML with JSON-LD `JobPosting` blocks, so no browser automation is needed. Crawled via category paths and sitemaps only, never keyword-search URLs (which BrighterMonday and MyJobMag both disallow in robots.txt).

**Global** — Greenhouse and Ashby public board APIs are the backbone: first-party employer data, hundreds of fully-described postings per request, from a bootstrapped list of confirmed company boards. Hacker News "Who is hiring?" via Algolia provides the historical spine — 33 unbroken monthly threads from January 2024. Arbeitnow, Jobicy and Himalayas add remote-market breadth.

Two rules apply to the global sources specifically. **ATS boards are not a history source**: 84% of their open postings are from the current year, and the older tail is survivorship-biased toward evergreen and hard-to-fill roles, so it is excluded from trend analysis. And **Remotive is disabled** — its robots.txt disallows the API path it publishes docs for, and this project honours robots without exceptions.

**Historical** — Wayback Machine replay, which retains the full JSON-LD including a `datePosted` that is often older than the archive date.

**Excluded by design** — LinkedIn, Indeed, Glassdoor, ZipRecruiter. Anti-scraping terms, active blocking and CAPTCHAs.

## Two rules that make the numbers trustworthy

1. **Share, never raw counts.** Corpus size differs wildly between years and segments, so every trend is reported as the percentage of that segment's postings mentioning a skill. Raw counts appear only as sample-size annotations.
2. **Thin cells are suppressed.** Any (skill, year, segment) cell built on fewer than 100 postings is excluded from trend claims. This is what stops the sparse 2024 sample producing a confident-looking lie.

## Getting started

```bash
uv venv && uv pip install -e ".[dev]"
```

```bash
uv run pytest
```

```bash
uv run jobradar sources
```

Check whether a URL may be crawled before planning against it:

```bash
uv run jobradar robots "https://www.brightermonday.co.ke/listings/some-job-abc123"
```

See what has been collected, and which year/segment cells are usable:

```bash
uv run jobradar status
```

## Build status

| Phase | State |
|---|---|
| 1. Fetch core — robots gate, rate limiter, cache, store, CLI | ✅ done |
| 2. Global adapters — Greenhouse, Ashby, HN, remote APIs | ✅ done |
| 3. Kenyan adapters — Fuzu, BrighterMonday, MyJobMag, JobWebKenya | pending |
| 4. Extraction + taxonomy | pending |
| 5. Historical backfill (Wayback) | pending |
| 6. Diffusion engine | pending |
| 7. Watchlist + track briefs | pending |

## Crawling conduct

- **robots.txt is enforced in code**, not by convention — a disallowed URL raises rather than fetches.
- Traffic is identified honestly with a contact URL. No rotating browser-UA spoofing.
- Default rate is one request per two seconds per domain, far below normal user load.
- Public listing pages and public APIs only. No logins, paywalls or CAPTCHA solving.
- Aggregate use only: we publish skill statistics, never republished listings. No candidate PII is collected.

## Prior art

Structure and taxonomy approach borrow from [`on-demand-tech-skills`](../on-demand-tech-skills). The document-collection inversion described above is the main departure.
