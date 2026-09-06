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

**Kenya** — BrighterMonday, MyJobMag and Fuzu. All serve server-side HTML with JSON-LD `JobPosting` blocks, so no browser automation is needed. Crawled via category paths only, never keyword-search URLs (which BrighterMonday and MyJobMag both disallow in robots.txt). A JobWebKenya adapter exists but ships disabled: it declares `Crawl-delay: 60`, which costs hours of wall-clock for postings the other three already cover.

**Global** — Greenhouse and Ashby public board APIs are the backbone: first-party employer data, hundreds of fully-described postings per request, from a bootstrapped list of confirmed company boards. Hacker News "Who is hiring?" via Algolia provides the historical spine — 33 unbroken monthly threads from January 2024. Arbeitnow, Jobicy and Himalayas add remote-market breadth.

Two rules apply to the global sources specifically. **ATS boards are not a history source**: 84% of their open postings are from the current year, and the older tail is survivorship-biased toward evergreen and hard-to-fill roles, so it is excluded from trend analysis. And **Remotive is disabled** — its robots.txt disallows the API path it publishes docs for, and this project honours robots without exceptions.

**Historical** — Wayback Machine replay. This is the *only* source of Kenyan history: live boards delete expired postings, so a fresh Kenyan crawl returns 2026 and nothing else. Archived pages still carry the site's JSON-LD, so the same parser reads them, and crucially `datePosted` is often older than the archive date — which is how 2024 postings surface at all.

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

## What is collected

As of the Phase 2 run (September 2026) — **36,782 global postings**, 98–100% carrying a
substantial description and 100% carrying a real publish date:

| source | postings | role |
|---|---:|---|
| Greenhouse | 16,222 | first-party employer boards (197 companies) |
| Hacker News | 10,601 | historical spine — 33 unbroken months from Jan 2024 |
| Ashby | 8,309 | first-party employer boards (176 companies) |
| Himalayas | 1,200 | remote-market breadth |
| Arbeitnow | 250 | remote-market breadth |
| Jobicy | 200 | remote-market breadth |

Trend-usable history, after excluding the survivorship-biased ATS tail:
2024 — 3,900 · 2025 — 4,022 · 2026 — 2,679. All three clear the 100-posting
threshold, so the global year-over-year comparison is sound.

The mix is deliberately weighted toward first-party employer data (67%) rather
than aggregators. Remote-only boards are held to a small share on purpose: they
skew hardest toward modern stacks, and over-weighting them would inflate the
diffusion gap for every skill at once.

## First findings

Global demand, share of technical postings, from the unbroken Hacker News monthly series:

| skill | 2024 | 2025 | 2026 | change |
|---|---:|---:|---:|---:|
| AI Agents | 1.8% | 11.2% | 18.5% | **+16.7pp** |
| Claude | 1.0% | 2.4% | 8.8% | +7.8pp |
| PostgreSQL | 16.5% | 19.5% | 24.1% | +7.7pp |
| LLM | 11.4% | 15.3% | 17.5% | +6.1pp |
| JavaScript | 7.7% | 5.1% | 4.2% | −3.5pp |
| Machine Learning | 20.4% | 19.3% | 18.6% | −1.9pp |

Two skills currently classify as **teach-ahead** — large and rising globally, near-absent in Kenya: **AI Agents** and **Claude**, both in the AI Engineering track. Four classify as **kenya_specific**, led by **Excel at 31.8% of Kenyan technical postings against 3.9% globally**.

Both sets are marked `provisional`: without Kenyan history, `teach_ahead` and `global_only` cannot be fully separated. See the caveat in [PLAN.md](PLAN.md).

## The finding that mattered

The project began from a stated premise: skills that rise globally trickle down to Kenya after a lag, so teaching ahead of the local market buys a head start. **The data does not support it.**

Of the skills that rose globally in 2024→2025, 82.6% also rose in Kenya — but 74.2% of *all* skills rose in Kenya over the same window, so the lift is only **+8.5 points**. Same-year rank correlation is **+0.25**; applying a one-year lag gives **−0.20**, making the forecast worse than chance.

What is real is the **level** gap. AI Engineering skills appear in 39.3% of global technical postings and 3.7% of Kenyan ones, and that gap is not closing year on year. Kenya reads as a *different* market rather than a delayed one — which makes the programme mix a strategic choice, not a timing one.

## Build status

| Phase | State |
|---|---|
| 1. Fetch core — robots gate, rate limiter, cache, store, CLI | ✅ done |
| 2. Global adapters — Greenhouse, Ashby, HN, remote APIs | ✅ done |
| 3. Kenyan adapters — Fuzu, BrighterMonday, MyJobMag | ✅ done |
| 4. Extraction + taxonomy — 147 skills, all mapped to a Zindua track | ✅ done |
| 5. Historical backfill (Wayback) — the only source of Kenyan history | ✅ done |
| 6. Diffusion engine — calibrated gap, status classification, watchlist | ✅ done |
| 7. Watchlist + track briefs | ✅ done |

## Crawling conduct

- **robots.txt is enforced in code**, not by convention — a disallowed URL raises rather than fetches.
- Traffic is identified honestly with a contact URL. No rotating browser-UA spoofing.
- Default rate is one request per two seconds per domain, far below normal user load.
- Public listing pages and public APIs only. No logins, paywalls or CAPTCHA solving.
- Aggregate use only: we publish skill statistics, never republished listings. No candidate PII is collected.

## Prior art

Structure and taxonomy approach borrow from [`on-demand-tech-skills`](../on-demand-tech-skills). The document-collection inversion described above is the main departure.
