# Job Market Skills Radar — Implementation Plan

**Purpose:** Measure which skills, languages and frameworks employers actually ask for — globally and in Kenya — and use the **global signal as a leading indicator** for what Zindua School should teach before the Kenyan market asks for it.

**Status:** Revision 2. Decisions in §11 are settled; this is the build spec.

**Core thesis (stated as a hypothesis, not an assumption):** skills that rise in global demand trickle down to the Kenyan market after some lag. If that lag is real and measurable, Zindua can teach ahead of local demand instead of reacting to it. §11a covers how we *test* this rather than take it on faith.

---

## 1. What this has to answer

The output is not "a scraper". It is a recurring evidence base that answers questions a curriculum committee actually asks:

| Question | Output that answers it |
|---|---|
| **What should we add to the curriculum before Kenyan employers ask for it?** | **Teach-ahead watchlist — the headline deliverable (§6)** |
| **How long does a skill take to travel from global to Kenyan job ads?** | Diffusion lag estimate, `skill_diffusion` frame |
| **Which global trends never arrive in Kenya, and why?** | Diffusion status classification |
| Is React still the safe frontend bet in Nairobi, or is Next.js overtaking it? | `skill_year`, Kenya, frontend category |
| Are we right to make LangChain/LangGraph the AI Engineering spine? | LangChain vs. raw APIs vs. n8n, global-leading vs. Kenya-current |
| Should Data Analytics teach Power BI or Tableau first? | Head-to-head mention share, Kenya |
| What do Kenyan employers pair with Python? | Co-occurrence frame |
| Is Cybersecurity Core sized right? | Postings-per-track volume over time |

Every skill row carries a `zindua_track` tag, so counts roll up onto your actual programmes — Software Engineering Core, Data Science Core, Cybersecurity Core, Data Analytics, Frontend Development, DevOps Engineering, Data Engineering, AI Engineering, and the short courses (Build with AI, AI Workflow Automation, Data Storytelling, Product Management, DSA). *Confirmed as matching your current programme list.*

---

## 2. What I verified before writing this

I probed the real sources rather than assuming.

### Kenyan boards — scrapable and structured

| Site | Status | Structured data | robots.txt constraint |
|---|---|---|---|
| BrighterMonday | Static HTML, 200, ~600KB category page | ✅ JSON-LD `JobPosting`: `title`, `description` (6.5k chars), `datePosted`, `validThrough`, `employmentType`, `industry`, `occupationalCategory`, `jobLocation`, `hiringOrganization` | Blocks `/job/`, `/api/`, and **all** query strings (`?q=`, `?page=`, `?keywords=`). **Allows** `/listings/*` detail pages, and `page=2`–`page=7` explicitly |
| MyJobMag | Static HTML, 200 | ✅ JSON-LD `JobPosting` with `datePosted` + `validThrough` | Blocks all query strings (`/*?`). Path pagination (`/jobs-by-field/information-technology/2`) allowed |
| Fuzu | Static HTML after redirect (`/kenya/jobs` → `/kenya/job`), 465KB | ✅ `ld+json` present | Most permissive; blocks 3 API paths. Publishes gzipped per-country sitemaps |

**Consequence:** we crawl category paths and sitemaps, **never keyword-search URLs**. Robots-compliant and cheaper. No Playwright needed — all three serve full server-side HTML.

**Two implementation notes found while smoke-testing the fetch core against a live BrighterMonday listing:**

- The JSON-LD is a `@graph`, and `hiringOrganization` is an **`@id` reference into that graph**, not an inline object. A naive `jp["hiringOrganization"]["name"]` yields `None`. The Phase 3 parser must resolve `@id` references across the graph — and because `company` feeds the cross-board dedupe key, getting this wrong would silently under-merge duplicates.
- `datePosted` (the employer's publish date, e.g. `2026-07-08`) differs from the page-level `datePublished` (`2026-09-06`). **`datePosted` is the correct trend axis**; `datePublished` tracks page regeneration and would smear every posting into the scrape month.

**A third, found while building the robots gate — see §12.**

### Global sources — first-party employer data is the backbone

| Source | Probe result | Role |
|---|---|---|
| **Greenhouse** `boards-api.greenhouse.io/v1/boards/{co}/jobs?content=true` | **200 — 615 jobs / 4.7MB from Stripe in one request.** Fields: `title`, `content` (5.6k chars), `first_published`, `updated_at`, `location`, `departments`, `offices` | **Primary global backbone.** First-party, no aggregator bias, one request per company |
| **Ashby** `api.ashbyhq.com/posting-api/job-board/{co}` | 200 — 2.4MB (Ramp) | Secondary backbone, startup/scale-up skew |
| **HN "Who is hiring?"** via Algolia | 200 — monthly threads, 340–600 comments each, complete 2024 → 2026 | **Historical spine.** The only clean, free, unbroken longitudinal global corpus found |
| Remotive · Arbeitnow · Jobicy · Himalayas | All 200, no key | Remote-market breadth, current snapshot |
| Lever `api.lever.co/v0/postings/{co}` | **404 on every company tested** (netlify, figma, plaid, mixpanel) — endpoint appears retired | **Dropped** |
| LinkedIn · Indeed · Glassdoor · ZipRecruiter | Your previous repo already hit non-200 on Glassdoor and ZipRecruiter | **Excluded by design** — ToS + active blocking + CAPTCHAs |

**One request per company** is what makes the global side scale without rate-limit risk.

### What Phase 2 actually found when built

Four things changed the design, and all four are the kind that would have quietly corrupted the analysis if found later.

**1. The ATS "history" is survivorship-biased — do not use it for trends.** Greenhouse and Ashby carry a real `first_published` date, so a currently-open board *looks* like it supplies history. Measured across four boards (204 postings):

| year | share of open postings |
|---|---|
| 2026 | 84.3% |
| 2025 | 8.3% |
| 2024 | 2.0% |
| ≤2023 | 5.5% |

A 2024 posting still open in 2026 is an evergreen, hard-to-fill or perpetually-reposted role — not a sample of 2024 demand. Treating it as one would measure *recruiting difficulty* and label it *skill demand*. ATS postings older than `ats_history_max_age_months` (6) are therefore counted in current-state depth but **excluded from year-over-year trends**. Only Hacker News and Wayback feed the historical series.

**2. Hacker News delivered exactly as hoped.** 33 unbroken monthly threads, January 2024 → September 2026, ~250 job posts each (**10,600 postings collected**), each dated to its own thread month with no smearing, company name parsed on 93–95%. This is the historical spine, and it is the reason the thin Kenyan history is survivable.

**3. Remotive is dropped.** Its robots.txt contains `Disallow: /api/*` — covering the very endpoint it publishes documentation for. The robots gate caught it on the first live run. There is a genuine tension (they grant API access in docs while disallowing the path to crawlers), but this project treats robots as a hard gate with no per-source exceptions, and their terms are separately restrictive (~4 requests/day, no republishing, paid tier from $5k/mo). Arbeitnow, Jobicy and Himalayas cover the same ground.

**4. African employers barely use these ATS platforms** — only 9 of 73 candidates resolved to a Greenhouse or Ashby board (Moniepoint, Decagon, One Acre Fund, Turing, Andela, Carbon, Jumia, Branch, Zola). That is a structural fact about the market, not a gap in the list, and it confirms the local signal must come from the Kenyan boards in Phase 3 rather than from ATS data. The nine that do exist remain valuable as bridge cases for the §6 calibration.

**Attribution obligations.** Jobicy requires a credit line with a direct link in any published report. This is tracked in `config/sources.yaml` and surfaced by each adapter's `attribution()`.

### What Phase 3 found on the Kenyan side

Three defects, and the striking thing is that **all three caused total silent data loss** — no exception, no empty-result signal, just a table that never gained rows. This is the failure mode this project is most exposed to, and it is why every parser now has a regression test pinned to real captured markup.

**1. MyJobMag's JSON-LD is not valid JSON.** It embeds raw CR/LF control characters inside string values, which the spec forbids and `json.loads` rejects outright. Strict parsing returned **zero blocks for every posting on the site** — the entire source lost, silently. Parsing is now lenient in three stages (strict → `strict=False` → strip illegal control characters).

**2. `hiringOrganization` is an `@id` reference, not an inline object.** Flagged in Phase 1, confirmed here: a naive read returns `None`. Because `company` feeds the cross-board dedupe key, losing it stops the same job appearing on BrighterMonday and MyJobMag from collapsing — inflating counts rather than obviously breaking.

**3. `Crawl-delay` was read but never applied.** The Phase 1 gate exposed `crawl_delay()` and nothing called it. JobWebKenya declares **`Crawl-delay: 60`** — 24× slower than our default — so we were reading robots.txt while ignoring the one directive that asks us to slow down, which made the "hard gate" claim hollow. The client now takes the stricter of configured rate and declared delay.

Two smaller corrections:

- **Speculative pagination was 404-ing against ourselves.** Fanning out to a fixed page count requested pages that do not exist — BrighterMonday's `software-data` category holds three pages, not seven — filling the failure table with our own mistakes rather than real problems. Pagination now advances one page at a time and stops when a page yields no postings.
- **Remote roles must not be read as on-site.** schema.org signals remote with `jobLocationType: TELECOMMUTE` and frequently omits `jobLocation` entirely. Treating an absent location as on-site would have biased the remote share downward across the whole Kenyan corpus. `applicantLocationRequirements` supplies the geography instead.

**Unexpected bonus:** BrighterMonday publishes `baseSalary` as a structured MonetaryAmount in KES on some postings. Kenyan salary data is scarce enough that this is worth having, and it is now captured.

**Fuzu access note.** Its gzipped sitemaps sit behind a Cloudflare challenge (HTTP 403), but ordinary category pages serve fine to our identified user-agent. Category pages carry an `ItemList` JSON-LD naming each posting, which is more robust than anchor scraping. Note the URL trap: categories are `/{country}/job/{slug}` (singular) while postings are `/{country}/jobs/{slug}` (plural) — conflating them yields a crawl that finds no postings at all.

---

## 3. The key architectural change from `on-demand-tech-skills`

Your existing scraper asks each site *"how many results for `"web development" "python"`?"* and reads the count. That's **N_skills × N_sites requests** for one integer per skill.

The new design **collects job documents once, then extracts skills offline.**

```
OLD:  60 skills × 3 sites = 180 requests → 180 integers, no dates, no text
NEW:  ~400 API calls + ~3,000 KE pages  → full text + dates + company + location
                                        → skills re-derivable forever, free
```

Why this matters:
- **Year comparison becomes possible at all.** Counts carry no date. Documents do.
- **Changing the taxonomy costs zero requests.** Add "LangGraph" next month, re-run extraction over stored text.
- **The diffusion analysis is only possible this way** — it needs the same skill measured on both sides over time.
- **Result counts are unreliable anyway** — rounded, capped, session-dependent. The old code's `except: append(0)` also silently zeroed real skills.

Kept from the old repo: the CSV-driven taxonomy, the `Skill,Category` shape, the tidy-frame-then-visualise split, notebook-friendly outputs.

---

## 4. The 2024 problem — settled

Live boards delete expired postings. Scraping today (September 2026) gives 2026, a thin tail of 2025, and almost nothing from 2024.

I verified the Wayback fallback works: archived BrighterMonday pages **retain the full JSON-LD `JobPosting`**, and `datePosted` is often older than the archive date (one 2024-archived page carried `datePosted: 2023-09-05`), so 2025-archived pages still yield 2024-posted jobs. But coverage is lopsided — measured via the CDX API, unique listing URLs, HTTP 200:

- **2025 archive crawls: ~19,800 BrighterMonday listings** — rich.
- **2024 archive crawls: ~7 listings** — negligible.

**Settled position (approved):** the deliverable is **2026 depth + 2025 comparison + 2024 directional signal**, not three equal years. Enforced in code: any (skill, year, segment) cell with `n_jobs_total < 100` is greyed out and excluded from trend claims.

**Adzuna is dropped** — 2024 isn't material enough to justify the key. Note for later: Adzuna also covers Kenya *currently*, so if the KE sample ever looks thin for reasons unrelated to history, it's worth reopening on those grounds.

The Kenyan historical thinness is survivable precisely because global carries the trend load — which is the point of §11a.

---

## 5. Architecture

Six layers, each independently runnable and testable.

```
┌─ 1. FETCH ─────────────────────────────────────────────────┐
│  Polite HTTP core: robots gate, per-domain token bucket,    │
│  retry/backoff, SQLite response cache, resumable queue      │
└──────────────────────┬─────────────────────────────────────┘
                       ▼
┌─ 2. SOURCE ADAPTERS ───────────────────────────────────────┐
│  One module per source. Same interface:                     │
│    discover() -> job URLs / API pages                       │
│    parse(raw) -> RawJob                                     │
│  KE:     fuzu · brightermonday · myjobmag · jobwebkenya     │
│  GLOBAL: greenhouse · ashby · hn_hiring · remotive ·        │
│          arbeitnow · jobicy · himalayas                     │
│  wayback (wraps any KE adapter for historical replay)       │
└──────────────────────┬─────────────────────────────────────┘
                       ▼
┌─ 3. STORE ─────────────────────────────────────────────────┐
│  SQLite = canonical raw store (full HTML/JSON + fetch meta) │
│  Content-hash dedupe. Nothing re-fetched needlessly         │
└──────────────────────┬─────────────────────────────────────┘
                       ▼
┌─ 4. NORMALISE + EXTRACT ───────────────────────────────────┐
│  Clean text → parse dates → dedupe cross-board →            │
│  regex skill extraction → seniority → remote flag → salary  │
└──────────────────────┬─────────────────────────────────────┘
                       ▼
┌─ 5. AGGREGATE ─────────────────────────────────────────────┐
│  skill_year · cooccurrence · **skill_diffusion**            │
└──────────────────────┬─────────────────────────────────────┘
                       ▼
┌─ 6. REPORT ────────────────────────────────────────────────┐
│  **Teach-ahead watchlist** · track briefs · notebooks       │
└────────────────────────────────────────────────────────────┘
```

The fetch layer is the only code that touches the network — that's what makes rate-limit policy enforceable in one place.

---

## 6. The diffusion engine — new headline deliverable

Because global is now the leading indicator, the analysis centre of gravity moves here.

### The frame: `skill_diffusion`

One row per skill, per snapshot:

| column | meaning |
|---|---|
| `skill`, `category`, `zindua_track` | |
| `global_share_now` | % of global postings mentioning it, latest window |
| `global_trend_12m` | change in global share over trailing 12 months |
| `kenya_share_now` | % of KE postings mentioning it |
| `kenya_trend_12m` | change in KE share |
| `diffusion_gap` | `global_share_now − kenya_share_now` |
| `estimated_lag_months` | cross-correlation of the two series (null until series are long enough) |
| `status` | classification below |
| `confidence` | driven by sample size on the thinner side |

### Status classification

| Status | Pattern | Curriculum meaning |
|---|---|---|
| **`teach_ahead`** | High + rising globally, low in KE | **Add now.** The watchlist |
| `established_both` | High in both | Core curriculum, keep |
| `arriving` | Rising in both, KE lagging | Validates the thesis — expand |
| `kenya_specific` | High in KE, low globally | Local necessity (M-Pesa/Daraja, USSD, Africa's Talking) — global signal is blind here |
| `global_only` | High globally, flat in KE across years | Trickle-down **failed** — don't teach on global signal alone |
| `declining_both` | Falling in both | Retire candidate |
| `insufficient_data` | Either side below threshold | Stated, not guessed |

`global_only` is the most important category, and it exists to protect you. Some global demand never arrives — scale-dependent tooling (large-cluster Kubernetes), US-specific compliance stacks, or tech tied to company sizes Kenya doesn't yet have. **Distinguishing `teach_ahead` from `global_only` is the single most valuable thing this project produces**, and it's the reason the thesis gets tested rather than assumed.

### Handling the known bias — stated, not hidden

Every global source skews *ahead* of the Kenyan market: Greenhouse/Ashby are US-tech-heavy, HN is startup-heavy, remote boards are modern-stack-heavy. This inflates the apparent `diffusion_gap` for essentially every skill.

Two mitigations:
1. **Report gaps as ranks, not absolutes.** Which skills have the *largest* gap relative to other skills, rather than treating the raw percentage-point gap as a real quantity.
2. **A calibration baseline.** Compute the median gap across long-established skills (SQL, Git, Java, Excel) — skills we know are fully diffused. That median *is* the structural source bias. Subtract it. What's left above the baseline is genuine signal.

Without this correction the watchlist would just rank "how American is this technology", which would be worse than useless for curriculum planning.

---

## 7. Data model

**`jobs`** — one row per unique posting

| column | notes |
|---|---|
| `job_id` | stable hash of `source` + native id |
| `source`, `source_group` | `greenhouse` / `KE` \| `GLOBAL` |
| `title`, `title_normalised`, `role_family` | frontend, backend, data, devops, security, AI/ML, PM… |
| `company`, `location`, `country`, `is_remote` | |
| `date_posted`, `year`, `month`, `valid_through` | trend axis |
| `employment_type`, `seniority` | intern / junior / mid / senior / lead |
| `salary_min`, `salary_max`, `salary_currency` | sparse in KE; kept anyway |
| `description_text`, `description_hash` | cleaned plain text |
| `url`, `fetched_at`, `is_historical` | `is_historical` = via Wayback |

**`skills_taxonomy`** — the curriculum bridge, hand-maintained CSV

| column | example |
|---|---|
| `skill` | `LangGraph` |
| `canonical_name` | `langgraph` |
| `category` | `ai-framework` |
| `zindua_track` | `AI Engineering` |
| `match_pattern` | `\blang\s?graph\b` |
| `negative_context` | patterns that veto a match |
| `is_diffusion_baseline` | flags the calibration set (§6) |
| `first_seen_year` | for spotting genuinely new tech |

**`job_skills`** — long: `(job_id, skill, n_mentions, matched_in)`.
**`skill_year`** — `(skill, category, zindua_track, year, month, source_group, n_jobs_mentioning, n_jobs_total, pct_share)`.
**`skill_cooccurrence`** — `(skill_a, skill_b, year, source_group, n_jobs, lift)`.
**`skill_diffusion`** — as §6.

### Two rules that make or break this

**Rule 1 — report share, never raw counts.** If we hold 400 postings from 2024 and 3,000 from 2026, Python's raw count triples and means nothing. Every trend uses **`pct_share`**. Raw counts appear only as sample-size annotations.

**Rule 2 — suppress thin cells.** `n_jobs_total < 100` → greyed out, excluded from trend claims. This is what stops sparse 2024 data producing a confident-looking lie.

---

## 8. Skill extraction — the part that's easy to get wrong

Naive substring matching fails on exactly the skills you care about:

| Skill | Failure mode | Guard |
|---|---|---|
| `R` | Matches every letter R | `\bR\b` **and** a co-signal (`R Studio`, `R and Python`, `in R,`) |
| `Go` | "go-getter", "go the extra mile" | `\bGo(lang)?\b` + veto list of ~15 idioms |
| `C` | "C-level", grades | `\bC\b` + programming context in same sentence |
| `Swift` | "swift delivery", "swift response" | Veto adjectival use before a noun |
| `Rust` | Corrosion, in manufacturing roles | Veto if `role_family` is non-tech |
| `Excel` | "excel at", "excellent" | `\bExcel\b` not followed by `at\b` / `lent` |

The taxonomy therefore carries an explicit `match_pattern` **and** `negative_context` per skill, never a bare string. `tests/test_extraction.py` pins ~60 tricky sentences with expected outputs — the taxonomy is code and is tested like code.

**Aliases** are first-class: `postgres|postgresql|psql` → `PostgreSQL`; `node|nodejs|node.js` → `Node.js`.

**Validation gate:** hand-label 100 random postings (50 KE, 50 global), measure precision/recall, publish no trends until **precision ≥ 0.90**. This matters doubly here — extraction error that differs between KE and global sources would masquerade as a diffusion gap.

---

## 9. Repo layout

```
job-market-skills-radar/
├── PLAN.md                      ← this file
├── README.md
├── pyproject.toml
├── config/
│   ├── sources.yaml             ← per-source rate limits, categories, on/off
│   ├── companies.yaml           ← Greenhouse/Ashby board slugs
│   └── settings.yaml            ← paths, windows, thresholds
├── data/
│   ├── raw/jobs.sqlite          ← canonical store (gitignored)
│   ├── cache/http_cache.sqlite  ← response cache (gitignored)
│   └── processed/               ← jobs.parquet, skill_year.csv, skill_diffusion.csv (committed)
├── taxonomy/
│   ├── skills.csv               ← the curriculum bridge
│   └── role_families.csv
├── src/jobradar/
│   ├── fetch/          client.py  ratelimit.py  robots.py  cache.py
│   ├── sources/        base.py
│   │                   fuzu.py  brightermonday.py  myjobmag.py  jobwebkenya.py
│   │                   greenhouse.py  ashby.py  hn_hiring.py
│   │                   remotive.py  arbeitnow.py  jobicy.py  himalayas.py
│   │                   wayback.py
│   ├── parse/          jsonld.py  dates.py  salary.py  seniority.py
│   ├── store/          db.py  models.py  export.py
│   ├── extract/        skills.py  dedupe.py  normalise.py
│   ├── aggregate/      frames.py  trends.py  cooccurrence.py  diffusion.py
│   ├── report/         watchlist.py  track_brief.py
│   └── cli.py
├── notebooks/
│   ├── 01_data_quality.ipynb
│   ├── 02_global_signal.ipynb
│   ├── 03_kenya_skills.ipynb
│   ├── 04_diffusion_and_lag.ipynb     ← the thesis test
│   └── 05_curriculum_brief.ipynb
├── reports/
└── tests/
```

```bash
jobradar discover --sources ke --since 2024-01-01
jobradar fetch --sources global --max-companies 400
jobradar backfill --source brightermonday --years 2025   # Wayback
jobradar extract --taxonomy taxonomy/skills.csv
jobradar aggregate && jobradar watchlist
```

---

## 10. Build phases

| Phase | Deliverable | Definition of done |
|---|---|---|
| **1. Fetch core** | `fetch/` + SQLite store + CLI skeleton | Rate limiter, robots gate, cache have passing unit tests; fake-source crawl runs end to end |
| **2. Global adapters** ⬆ | Greenhouse, Ashby, HN Who's Hiring, 4 remote APIs + `companies.yaml` | ≥ 40,000 global postings; HN covers every month 2024-01 → present |
| **3. Kenyan adapters** | Fuzu, BrighterMonday, MyJobMag, JobWebKenya | ≥ 1,500 current KE postings with populated `date_posted` and `description_text` |
| **4. Extraction + taxonomy** | `skills.csv` (~150 skills, incl. diffusion baseline set) + extractor + tests | Precision ≥ 0.90 on 100 hand-labelled postings, checked separately for KE and global |
| **5. Historical backfill** ⬇ | Wayback adapter, 2025-focused | 2025 BrighterMonday + MyJobMag replayed; 2024 volume measured and reported honestly, including if too thin to use |
| **6. Diffusion engine** ⭐ | `diffusion.py` + calibration baseline + `skill_diffusion.csv` | Every skill classified; baseline correction applied; thesis backtest run (§11a) |
| **7. Watchlist + briefs** | `reports/` | Ranked teach-ahead watchlist + one brief per Zindua programme: add / expand / hold / retire |

**Phase 2 moved ahead of Phase 3**, and Phase 5 dropped below extraction — global is now the load-bearing series, and it's also the cheapest and most reliable to collect. Phases 2 and 3 remain independently parallelisable.

---

## 11. Decisions — settled

| # | Decision | Outcome |
|---|---|---|
| D1 | Scope of roles | **All postings**, filtered at analysis time. Non-tech demand for SQL/Excel/Power BI is directly relevant to Data Analytics and the short courses |
| D2 | Geography | **Collect the region** (KE + Uganda + Nigeria via the same Fuzu/MyJobMag parsers), **analyse Kenya first**. Near-zero extra code; gives a second East African comparison point |
| D3 | Adzuna API key | **No.** 2024 isn't material enough to justify it |
| D4 | Extraction method | **Regex + curated taxonomy for v1.** LLM pass in v2 purely as a *discovery* step proposing new taxonomy rows for human approval — never as the counting mechanism |
| D5 | Cadence | **Monthly.** The forward series is worth more than the backfill within a year — and it's what eventually makes `estimated_lag_months` real |
| D6 | Publication | **Internal first**, public "Kenya Tech Skills Report" kept open as an option |

### 11a. Testing the trickle-down thesis rather than assuming it

The curriculum strategy rests on global demand predicting Kenyan demand. That is a falsifiable claim, and D5's monthly cadence is what eventually lets us measure it properly. Two things happen before then:

**Now — cross-sectional backtest.** Using HN's complete 2024–2026 global series against the 2025 Wayback Kenya sample: take the skills that rose globally during 2024, and check whether they rose in Kenya during 2025–26. That yields a first, rough **hit rate for the hypothesis** — the share of global risers that actually arrived. If that number is high, the watchlist is trustworthy. If it's low, we learn that *before* betting a syllabus on it. Either result is worth having, and both are reportable.

**Later — longitudinal.** Once ~12 months of monthly collection accumulate on both sides, `estimated_lag_months` becomes a real cross-correlation rather than a null column. Until then it stays null rather than being fabricated from thin data.

Design consequence: `diffusion.py` ships the classification and the calibrated gap immediately; the lag estimate is deliberately left unpopulated until the data supports it.

---

## 12. Not getting rate limited

1. **Prefer APIs.** Greenhouse returns 615 jobs per request; the remote boards return hundreds. One request replaces hundreds of page fetches. This is most of the answer.
2. **Per-domain token bucket**, default **0.5 req/s** with ±30% jitter, configurable per source. Sequential within a domain, parallel across domains.
3. **Persistent response cache** (SQLite, 14-day TTL). Re-running after a parser fix costs **zero** requests.
4. **Exponential backoff with jitter** on 429/503, honouring `Retry-After`; three retries, then park in a `failed` table — never a silent `except: append(0)`.
5. **Honest identification.** Descriptive User-Agent naming the project with a contact URL, plus `Accept-Language: en-KE,en`. No rotating browser-UA spoofing.
6. **robots.txt as a hard gate**, parsed per domain and checked before every fetch. Given BrighterMonday's rules this is a real constraint, not a formality.

   ⚠️ **Do not use Python's stdlib `urllib.robotparser` here.** It treats a blank line as a rule-group terminator, which is non-conformant with RFC 9309. BrighterMonday's real robots.txt is shaped exactly that way — `User-agent: *`, blank line, then the rules — so the stdlib parser discards every directive and reports all URLs as allowed. That is a *silent* compliance failure: the crawl looks polite while ignoring the file entirely. Verified during Phase 1 and fixed by using **Protego** (the RFC-conformant parser Scrapy uses), which also handles the `*` and `$` wildcards these sites rely on. `tests/test_robots.py` pins the behaviour so it cannot regress.
7. **Resumable checkpointing.** URL queue in SQLite with per-URL state; kill at 60% and restart where it stopped.

Wayback gets a stricter bucket (**1 req/s**) and uses the `id_` raw-replay endpoint to skip the archive toolbar.

---

## 13. Legal and ethical position

- **robots.txt enforced in code.** Disallowed URLs raise rather than fetch.
- **Public listing pages and public APIs only.** No logins, paywalls, or CAPTCHA solving.
- **Aggregate use only.** We publish skill statistics, never republished listings.
- **Identified traffic** at rates far below normal user load.
- **Excluded by design:** LinkedIn, Indeed, Glassdoor, ZipRecruiter.
- Company names and job titles are business information; no candidate PII is ever collected.

---

## 14. Honest risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| **Global sources' US/startup skew inflates every diffusion gap** | **High** | §6 calibration baseline; report gaps as ranks, not absolutes |
| **Trickle-down thesis is weaker than assumed** | **Medium** | §11a backtest measures the hit rate explicitly; `global_only` status catches non-arrivals |
| 2024 too thin for trend claims | High (accepted) | Reframed and enforced by the <100 suppression rule |
| Kenyan series too short for a real lag estimate | High (accepted) | `estimated_lag_months` stays null until monthly collection matures |
| KE postings under-specify tech stack | Medium | Many KE listings say "web developer" with no stack. Track the *specification rate* itself — it's a finding, not just noise, and it biases KE share downward for every skill (a further reason for the §6 baseline) |
| Extraction accuracy differs KE vs. global | Medium | Precision measured separately per segment (§8) |
| A site changes HTML and breaks a parser | Medium | JSON-LD first; per-source parser tests; loud failures |
| Cross-board duplicate jobs inflate counts | Medium | Fuzzy dedupe on title + company + date proximity; report pre/post counts |
| Greenhouse/Ashby company list goes stale | Low | `companies.yaml` is versioned and reviewed each monthly run |
