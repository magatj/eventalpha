# EventAlpha

Personal-use AI investment research platform. EventAlpha collects news, future events and SEC filings, links them to companies, pulls prices (Massive, formerly Polygon.io) and SEC EDGAR financials, asks OpenAI for an evidence-cited thesis, and computes **bear/base/bull valuations with a separate quantitative engine**. Every forecast is stored as an immutable record and can be backtested.

> Research tool, not investment advice. Model targets are fundamental value estimates under stated assumptions. They are not predictions that a price will be reached by a date.

Everything runs on **Windows localhost** with Docker Desktop. No AWS account is needed. Terraform for a future AWS deployment is included but is **never applied automatically**.

---

## Quick start (Windows 11)

Prerequisites: Docker Desktop (running), Node.js 20+, Git. Python is not needed on the host because the backend runs in Docker with Python 3.12.

```powershell
# 1. first-time setup: creates .env (demo mode), builds images, installs frontend deps
.\scripts\eventalpha.ps1 setup

# 2. start PostgreSQL + migrations + API + worker + scheduler, then the Vite dev server
.\scripts\eventalpha.ps1 start

# 3. (optional, second terminal) fill the demo dashboard
.\scripts\eventalpha.ps1 seed-demo
```

| What | URL |
|---|---|
| Frontend | http://localhost:5173 |
| Backend API | http://localhost:8000 |
| API docs (OpenAPI) | http://localhost:8000/docs |
| PostgreSQL (host) | `localhost:5433` (5433 avoids clashing with a locally installed PostgreSQL on 5432) |

If PowerShell blocks scripts: `powershell -ExecutionPolicy Bypass -File .\scripts\eventalpha.ps1 start`.

### All commands

| Task | PowerShell | Make (Git Bash/WSL) |
|---|---|---|
| First-time setup | `.\scripts\eventalpha.ps1 setup` | `make setup` |
| Start everything | `.\scripts\eventalpha.ps1 start` | `make start` |
| Frontend only | `.\scripts\eventalpha.ps1 frontend` | `make frontend` |
| Stop services | `.\scripts\eventalpha.ps1 stop` | `make stop` |
| Run DB migrations | `.\scripts\eventalpha.ps1 migrate` | `make migrate` |
| Run all tests + lint | `.\scripts\eventalpha.ps1 test` | `make test` |
| Reset demo data | `.\scripts\eventalpha.ps1 reset-demo` | `make reset-demo` |
| Seed demo (run discovery) | `.\scripts\eventalpha.ps1 seed-demo` | `make seed-demo` |
| Logs | `.\scripts\eventalpha.ps1 logs [api\|worker\|scheduler\|db]` | `make logs` |
| Status | `.\scripts\eventalpha.ps1 status` | — |
| Validate Terraform (no apply) | `.\scripts\eventalpha.ps1 tf-validate` | `make tf-validate` |

The frontend can also run in Docker: `docker compose --profile frontend up -d frontend`.

---

## Demo mode vs. live mode

Toggle the mode in the header or on the Settings page. `DATA_MODE` in `.env` sets the startup default. Demo and live data are stored separately and never mixed. Demo mode shows a purple banner and **DEMO DATA** badges.

| | Demo | Live |
|---|---|---|
| Prices | synthetic random walks, anchored to synthetic fundamentals | Massive daily bars (needs `MASSIVE_API_KEY`) |
| Financials | synthetic statements | SEC EDGAR XBRL CompanyFacts (free; needs `SEC_USER_AGENT` with your email) |
| News | synthetic `[DEMO]` headlines | Massive news API (per ticker), SEC filings, RSS feeds |
| AI thesis | rule-based, never calls OpenAI | OpenAI if `OPENAI_API_KEY` is set, otherwise a labeled rule-based summary |
| External calls | none | yes, rate-limited and cached |

Live-mode keys go in `.env`, which is git-ignored and never sent to the browser:

```ini
DATA_MODE=live
MASSIVE_API_KEY=...            # https://massive.com (free "Stocks Basic" tier works: EOD, 2y, 5 req/min)
SEC_USER_AGENT=EventAlpha you@example.com
OPENAI_API_KEY=...             # optional
OPENAI_MODEL=gpt-5-mini        # pick an inexpensive model; update the per-token prices below
OPENAI_INPUT_PRICE_PER_M=0.25
OPENAI_OUTPUT_PRICE_PER_M=2.00
AI_MONTHLY_BUDGET_USD=10
```

Restart after editing `.env`: `docker compose up -d api worker scheduler`.

**Without a Massive key, live mode never invents prices.** SEC financials and news are still fetched and shown, but analysis stops with "No price data available … Set MASSIVE_API_KEY", and the UI shows an unavailable state.

---

## Architecture

```
React (Vite, :5173) ──/api proxy──► FastAPI (:8000) ──► PostgreSQL (:5433)
                                          │  enqueue                ▲
                                          ▼                         │
                                   analysis_jobs table ◄── Worker (python -m app.worker.main)
                                          ▲                         │ providers
                       Scheduler (APScheduler, separate service)    ├─ Massive (prices, splits, news)
                       enqueues time-slot jobs                       ├─ SEC EDGAR (CIK, filings, XBRL)
                                                                     ├─ RSS (SEC, Fed, BLS)
                                                                     └─ OpenAI (structured JSON)
```

* **Long work never blocks requests.** `POST /api/stocks/{t}/analyze` returns a `job_id`. The worker claims jobs with `SELECT … FOR UPDATE SKIP LOCKED`, and the UI polls `GET /api/jobs/{id}`.
* **No duplicate scheduled jobs.** The scheduler is its own process (not inside the reloading API). It holds a PostgreSQL advisory lock, so only one scheduler is active, and every run uses a unique slot key (e.g. `discovery:live:2026-10-12`).
* **The provider abstraction** (`app/providers/market/base.py`) defines `get_latest_price`, `get_historical_prices`, `get_company_profile`, `get_corporate_actions` and `get_market_indices`. Business logic only sees this interface. `app/services/providers.py` is the single place where demo and live implementations are chosen.
* **HTTP plumbing** (`app/services/http.py`): sliding-window rate limiter, retries with exponential backoff (honors `Retry-After`), timeouts, typed errors, and a response cache stored in PostgreSQL.

### Repository layout

```
backend/            FastAPI app, worker, scheduler, Alembic migrations, tests
  app/providers/    market (Massive, demo), sec (EDGAR client, XBRL normalizer), news (RSS, live, base)
  app/services/     analysis pipeline, forecasting engine, AI, events, discovery, backtest, jobs, prices
  app/api/          REST routes
  tests/            unit tests + integration tests (PostgreSQL); recorded SEC fixtures
frontend/           React + TypeScript + Tailwind v4 + shadcn-style components + Recharts + TanStack Query
infra/terraform/    modules (network, ecr, rds, ecs, s3-cloudfront, secrets, scheduler, monitoring) + envs (dev, prod)
scripts/            Windows helper (eventalpha.ps1), db init, AWS post-deploy
docs/               AWS deployment & cost guide
.gitlab-ci.yml      CI/CD (AWS stages disabled by default)
```

---

## How the analysis works

1. **Validate and resolve** the ticker against the SEC ticker→CIK map and the Massive ticker details. ETFs and other non-common-stock types are rejected.
2. **Prices.** Raw daily bars are stored, and split adjustment is applied at read time from the split history. A later split therefore never leaves stale adjusted data behind. Vendor corrections are logged to `market_data_revisions`. Staleness is measured in NYSE sessions (`exchange_calendars`, which covers holidays and early closes).
3. **SEC financials** (`app/providers/sec/xbrl.py`):
   * Each metric has a priority list of XBRL concepts, merged per period, because companies switch concepts over time.
   * Facts are classified by duration (quarter / half-year / nine-month / annual).
   * Year-to-date cash-flow facts are converted to quarters by subtraction (Q2 = H1 − Q1, Q4 = FY − 9M) and flagged as derived. They are never mixed in as quarters.
   * Per-share values are never derived.
   * Each fact keeps its accession number, form, filed date, unit and concept. Snapshots can be built "as filed on date X" (point-in-time), with no look-ahead.
   * Fiscal labels come from each filing's own reporting period, which handles fiscal years ending in May (NKE) or January (NVDA) correctly.
4. **News and events.** Inputs are Massive news, SEC filings (8-K items labeled) and RSS. URLs are canonicalized, and the same story from several outlets is merged into one event (title-token Jaccard similarity within 72h). Rumor language ("reportedly", "people familiar") marks an event *unconfirmed*. Companies are linked by exchange-ticker, cashtag, provider tag or name match, each with a confidence. Upcoming catalysts come from the Fed FOMC calendar (verified from federalreserve.gov) and **estimated** earnings dates based on 8-K Item 2.02 cadence, labeled "estimated".
5. **AI thesis** (`app/services/ai.py`):
   * The model gets numbered sources `[S1..Sn]` and must return a Pydantic-validated structure: claims typed as fact / guidance / analyst estimate / assumption / speculative, each with citations.
   * Unknown citations are stripped. Fact-type claims left with no valid source are rejected and listed.
   * The schema has **no price fields**. The model may only suggest small growth or margin adjustments (capped at ±5pp / ±3pp), and only when a valid source is cited.
   * Responses are cached by input hash, and a monthly USD budget is enforced before every call.
6. **Quantitative engine** (`app/services/forecasting/`, model `quant-2.0.0`), run for 3/6/12/24/36/60-month horizons. See [Valuation framework](#valuation-framework) below.
   * Outputs: P/E-growth and WACC-terminal-growth sensitivity grids, a separate historical-volatility 1σ band for short-term movement, and **no probabilities**. Scenario ranges are not calibrated confidence intervals.
7. **Persistence.** Each analysis stores its thesis, sources, snapshot and usage. Each horizon's forecast and scenarios are inserted as new rows. A PostgreSQL trigger blocks UPDATE/DELETE on forecast tables; only the demo reset bypasses it, for demo rows.

### Valuation framework

Every analysis stores a **valuation audit** (`analyses.valuation`). It holds the method and why it was chosen, company and peer multiples, bear/base/bull inputs, guardrail results, SEC provenance and data timestamps. It is shown on the stock page under **"How EventAlpha calculated this forecast"**.

**1. Method by business model** (`valuation_registry.py`). A method is activated only when its inputs exist in the filings. Otherwise the result is *limited* or *unsupported*; an unsuitable generic method is never applied silently.

| Business model | Method |
|---|---|
| Profitable operating / technology / industrial | Forward P/E + DCF (FCF after stock-based compensation) |
| Cyclical (energy, materials, shipping), depressed or inflated earnings | P/E on normalized (median) margins + DCF |
| Airlines | Normalized earnings + DCF with full aircraft capex (EV/EBITDAR unsupported: aircraft rent is not tagged consistently) |
| Utilities | P/E + DCF (DCF dropped while capex makes FCF negative) |
| Banks | P/TBV justified by sustainable ROTE (70%) + P/E (30%) |
| Insurers | P/B justified by normalized multi-year ROE (70%) + P/E (30%) |
| Other financials | P/E (FCF-based DCF not meaningful) |
| REITs | P/FFO (FFO = net income + D&A − property-sale gains, only when reported) |
| Unprofitable growth | Company/peer price-to-sales with an explicit margin path |

**2. Company-specific multiples** (`company_multiples.py`, `peer_comparison.py`):
* Historical multiples are measured point-in-time at each month-end, using SEC facts filed by that date and split-adjusted prices and per-share data.
* Months with negative, near-zero (net margin < 2%) or depressed earnings are rejected.
* Peers come from the same SIC industry (4 → 3 → 2 digits), nearest in size, margin and growth, using the stored market screen (no extra API calls).
* The company's own history comes first. Peers (30%) and fundamental anchors such as justified P/B (25%) are supporting evidence, and may move the anchor at most −20% / +25% from the company's own median.
* The broad sector band is used only when there is no history and no peers, and the analysis is then marked *limited*.
* The base multiple starts at today's multiple and moves toward the anchor with a 2-year half-life. The bear multiple never expands; the bull multiple never exceeds the evidence.

**3. Scenarios.** Each scenario varies revenue growth, net margin, EPS, FCF, capex, stock compensation, share count (bear +1pp dilution) and discount rate (bear +1pp):
* Recent growth is not extrapolated for cyclical or abnormal earners (capped at 15%).
* When YoY growth and the 3-year CAGR disagree by more than 20pp, the lower figure is weighted.
* Margins move to their scenario target over 2 years. No margin recovery is assumed while revenue is shrinking.
* When P/E and DCF values disagree, DCF weight fades from full at 1.5x to zero at 3x. Beyond that, DCF is shown only as a cross-check.

**4. Guardrails** (`valuation_guardrails.py`). Upside is **never capped**. A base-case upside above `VALUATION_UPSIDE_REVIEW_THRESHOLD` (100%) must pass every check:
* the multiple is within the company's historical median;
* the multiple evidence is company-specific;
* earnings are not depressed or inflated;
* the methods agree within 2x;
* the data passed validation.

A multiple expansion of more than 50% beyond the company's historical range also triggers review.

Statuses: **Valid**, **Limited data**, **Requires review**, **Unsupported valuation**, **Invalid inputs**.
* Only *valid* and *limited* results can be ranked or published.
* A limited result's valuation factor is pulled halfway toward neutral.
* Every audit record is kept.
* Unsupported and invalid results have their targets withheld.

**5. SEC data quality** (`financials.py`, `xbrl.py`):
* **Split adjustment.** Share and per-share facts filed before a later split are split-adjusted; filings made after the split are already restated.
* **Revenue for banks and insurers.** When no total-revenue concept is reported, revenue is built as net interest income + noninterest income (banks) or premiums + investment income (insurers).
* **Net income.** Net income to common is used for EPS. The consolidated `ProfitLoss` fallback is flagged.
* **Capex.** Capex includes separately tagged flight-equipment and oil & gas payments.
* **Stale data.** TTM components older than the latest income statement are ignored.
* **Validation.** Inconsistent data is classified (*invalid*, *data unavailable*, *unsupported accounting*), never scored as weak performance.

### AI research chat

Every stock page has an **AI Analyst** panel. It opens when you click a ticker anywhere: Dashboard, Discovery, Watchlist, Forecast History or Event Radar. The panel is tied to that ticker, so follow-up questions don't need to repeat it.

**What it answers from.** It explains and questions EventAlpha's stored data; it does not replace the forecasting engine.
* **Grounded context** (`services/chat/context.py`):
  * Only the selected ticker's records: the analysis snapshot and valuation audit, immutable forecasts, SEC facts, linked news and filings, and its discovery row.
  * Which records are included depends on the question.
  * The size is capped at `CHAT_MAX_CONTEXT_TOKENS`.
  * Each source has a label (`[S1]`) that answers must cite.
* **Read-only tools** (`services/chat/tools.py`), validated with Pydantic, bound to the conversation's ticker, and logged in `chat_tool_executions`:
  * `get_stock_fundamentals`, `get_latest_forecast`, `get_forecast_assumptions`, `get_recent_events`, `get_forecast_history`
  * `get_discovery_score` (optional comparison ticker)
  * `calculate_what_if_scenario`
* **What-if scenarios.** These re-run the real Python engine on the stored inputs with bounded overrides:
  * revenue growth ±30pp
  * net margin ±20pp
  * EPS −90% to +100%
  * multiples −60% to +100%
  * discount rate −3 to +5pp

  The result is labeled a **user-generated hypothetical** and is never written to the forecast tables.
* **Base-target attribution.** The base target is split into EPS growth versus multiple change, an exact additive split, plus the P/E-versus-DCF blend.

**Safeguards**
* **Answers are checked:**
  * Unknown citations are removed.
  * Dollar targets that match neither the official forecast nor a hypothetical run are flagged.
  * Requires-review, unsupported and stale forecasts are disclosed.
* **Forecast changes:** if a newer analysis replaces the one a conversation used, a notice appears before the answer.
* **Security:**
  * News and filing text is passed as `untrusted_text` and phrases that look like instructions are flagged. The model is told never to follow them.
  * Markdown is rendered without raw HTML, and only http(s) links are kept.
  * The OpenAI key stays on the backend.
  * Requests are rate-limited.
* **Cost:**
  * Uses a cheap model (`OPENAI_CHAT_MODEL`, low reasoning effort) and caps output tokens.
  * Older turns are folded into a summary, with no extra model call.
  * A per-month chat budget sits inside the overall AI budget.
  * Every call is recorded in `ai_usage` (purpose `chat`) and on the message.
  * Chat never triggers a discovery run or a data refresh. Stale data shows its date and a **Refresh data** button.

**API:**
* `POST /api/stocks/{ticker}/chat`: server-sent events `meta`, `notice`, `delta`, `tool`, `scenario`, `done`, `error`.
* `GET /api/stocks/{ticker}/chat/history`
* `DELETE /api/stocks/{ticker}/chat/history`: clears the conversation; spend records are kept.

Storage: `chat_conversations`, `chat_messages`, `chat_source_references`, `chat_tool_executions` (migration `0006`).

### Stock discovery (market-wide)

`DISCOVERY_UNIVERSE_MODE=dynamic` (the default) discovers stocks from the **whole US-listed market**. `fixed` keeps the original behaviour, limited to `backend/app/data/universe.txt`; that file now only acts as a seed of always-monitored names.

The progressive funnel:

| Stage | What happens | Data source (free-tier cost) |
|---|---|---|
| 1. Master universe | All CS/OS/ADR listings on NYSE, Nasdaq and NYSE American. Identity by FIGI; ticker changes keep history; delistings are kept as inactive | Massive `/v3/reference/tickers`, paginated (~9 calls, weekly) |
| 2. Market data | ~3 months of daily prices for every listed stock | Massive grouped daily: 1 call returns every ticker (63 calls first time, then 1/day) |
| 3. Fundamentals | Revenue, net income, operating income, cash flow, capex, cash, debt, equity, shares for ~6,000 filers | SEC XBRL **frames**: 1 call per metric for all filers (~20 free calls/day) |
| 4. Events | Market-wide news and every 8-K/10-Q/10-K filed. Can introduce stocks never seen before | Massive market news (1 call/hour) + SEC daily form index (1 free call/day) |
| 5. Screening | Eligibility: market cap ≥ $500M, 20-day average dollar volume ≥ $20M, price ≤ 5 sessions old, ≥ 20 sessions, usable SEC financials. Smaller caps qualify only with an exceptional confirmed catalyst. ADRs are flagged as needing alternative fundamentals | stored data (sector from SEC SIC, looked up once) |
| 6. Selection (60) | 25 financial leaders, 15 valuation opportunities (sector-relative earnings/FCF/EBIT yields; banks earnings-only; REITs excluded), 15 new catalysts, 5 underexplored. Deduplicated and refilled; ties broken by a daily seeded hash, never alphabetically | — |
| 7. Deep analysis | Existing forecast engine (P/E, DCF, scenarios) for the 60; no AI | per-stock Massive calls (~5 the first time, mostly cached afterwards) |
| 8. AI | Top 10 only; forecasts and scores recomputed after AI adjustments | OpenAI (budget-capped) |
| 9. Publish | Up to 20 stocks meeting `DISCOVERY_MIN_PUBLISH_SCORE` (may be fewer) | — |

Every run stores a row for **every screened security**: eligibility, exclusion reason, pathway(s), point-in-time screening metrics, catalyst coverage, score factors, AI status, model versions and data timestamps. The Discovery page shows the funnel counts, nine filters, a **"why EventAlpha discovered this stock"** column, and an **Audit CSV** download (`GET /api/discovery/audit.csv`).

**Scoring:** a six-factor 0–100 score with an explicit formula (`app/services/discovery_scoring.py`):
* Positive factors: financial strength 25%, valuation 25%, catalyst 20%, data freshness 10%.
* Penalties: forecast uncertainty −10% and downside risk −10%.

Unknown factors are excluded and the remaining weights renormalized, so missing data is never treated as zero. Ranking uses full precision; the 5-point rounding is for display only. Upside alone doesn't determine rank.

**Backtesting** (Forecast History page):
1. *Realized*: matured stored forecasts are compared with split-adjusted realized returns and SPY. Tickers that lost price coverage are listed as unresolved, not dropped, to avoid survivorship bias.
2. *Point-in-time replay*: the engine is re-run at past dates using only prices and SEC facts available then, without AI.

### Scheduled jobs (America/New_York)

| Job | Schedule |
|---|---|
| News ingestion (market-wide + rotating per-stock checks) | every 60 min |
| Price refresh for analyzed stocks (one grouped-daily request) | Mon–Fri 16:45, NYSE sessions only |
| Market data + screening refresh (grouped daily + SEC frames) | Mon–Fri 17:05, sessions only |
| SEC filing monitor (monitored names + market-wide daily index) | Mon–Fri every 2h 07:10–19:10 |
| Catalyst AI interpretation (unchanged events skipped) | Mon–Fri 17:30, sessions only |
| Discovery | Mon–Fri 18:00, sessions only |
| Forecast review (watchlist) | Saturday 09:00 |
| Master universe refresh | Sunday 06:00 |
| Cache cleanup | daily 03:15 |

All thresholds, quotas and limits are environment variables (see `.env.example`, section "Stock discovery").

---

## Testing

```powershell
.\scripts\eventalpha.ps1 test
```

* **Backend: 116 tests** (pytest), covering:
  * HTTP retries, backoff and caching; Massive parsing and errors.
  * Split adjustment; XBRL normalization, including real recorded NKE/NVDA SEC data.
  * YTD derivation, restatements and point-in-time snapshots; fiscal labels.
  * Missing, unusual and non-USD statements; ticker validation and incorrect mappings.
  * Event dedup, rumors and association.
  * Revenue/EPS/P/E/DCF math; scenario ordering; negative earnings and marginal profitability.
  * NYSE holidays, early closes and stale prices; scheduler slots.
  * AI citation guardrails, caching and budget (fake client); ranking.
  * PostgreSQL integration: end-to-end demo slice, cache reuse, forecast immutability trigger, job dedup and SKIP LOCKED, watchlist, live mode without a price key, discovery, backtest replay, demo reset.
* **Frontend: 8 tests** (Vitest + Testing Library), plus ESLint and `tsc`.
* Tests never call paid or external APIs. SEC fixtures are recorded real responses (trimmed); Massive fixtures follow the documented response shapes.

---

## Verification status (honest)

| Integration | Status |
|---|---|
| Demo mode, end to end (UI → API → worker → engine → PostgreSQL) | ✅ run and tested |
| SEC EDGAR (ticker map, submissions, CompanyFacts) | ✅ live-tested on NKE, NVDA, MSFT, TSM, JPM |
| RSS feeds (SEC press releases, Federal Reserve, BLS) | ✅ live-tested |
| Live mode without a Massive key (fails safely, keeps SEC data) | ✅ integration-tested |
| **Massive API** | ⚠️ **not tested against the live service** (no key available). The client follows the documented endpoints (`/v2/aggs/...`, `/v3/reference/tickers`, `/v3/reference/splits`, `/v2/reference/news`, grouped daily) with base URL `https://api.massive.com`, and is tested against shaped fixtures. Verify with your key; Settings shows provider errors clearly. |
| **OpenAI** | ⚠️ **not tested against the live API** (no key). Uses `client.responses.parse(text_format=…)` from openai 3.29. Both schemas were verified to convert to OpenAI strict JSON schema, and caching and budget logic are tested with a fake client. Confirm that the `OPENAI_MODEL` you choose exists and update the token prices. |
| Terraform | ✅ `fmt` + `validate` pass for dev and prod (Terraform 1.10.5, AWS provider 5.x). **Never planned or applied.** |
| GitLab CI | ⚠️ written, not executed (no GitLab runner here) |

## Known limitations

* Free Massive tier: end-of-day prices only (no real-time quotes), about 2 years of history, and 5 requests/minute. A live discovery run over many candidates can take a long time; tune `DISCOVERY_MAX_CANDIDATES`. The dashboard shows actual index levels: S&P 500 and Dow Jones official closes from FRED (free, no key; usually published the evening of the session or the next morning) and the Nasdaq-100 from Massive (included in the free plan). An ETF price appears only as a labeled fallback if an index level can't be fetched.
* Company-specific press releases depend on the Massive news endpoint. The default RSS feeds are macro/regulatory only. Add investor-relations RSS feeds via `RSS_FEEDS` (comma-separated). They are fetched only if robots.txt allows it.
* Earnings dates are estimates from filing cadence unless a confirmed source is added.
* Event impact and materiality are keyword heuristics unless the AI catalyst job has interpreted the event.
* Sector P/E and EV/Sales bands are heuristic model assumptions, used only as a last resort (and then marked *limited*).
* Company valuation history covers about 2 years (the free Massive tier's price history), which spans less than a full business cycle.
* Successor registrants are not linked: when the SEC ticker map moves a ticker to a new CIK (e.g. a holding-company reorganization), the predecessor's XBRL history is not used, so the valuation can be *unsupported* until the new filer has four quarters of data.
* Screening fundamentals (frames API) are the latest restated fiscal-year values, not point-in-time. Deep analysis is point-in-time.
* Foreign issuers (20-F filers, e.g. TSM) show financials, but per-share valuation is disabled (no ADR ratio).
* The rate limiter is per process. The API and worker can each use the Massive quota; on a 429 the client backs off and honors `Retry-After`.
* The UI has no authentication; it is designed for localhost personal use. Add auth before exposing it publicly.

## Dependency versions (resolved Oct 2026)

Backend (Python 3.12): FastAPI 0.143, Pydantic 2.14, SQLAlchemy 2.0.54, Alembic 1.20, psycopg 3.3, pandas 3.0, NumPy 2.5, httpx 0.28, APScheduler 3.11, openai 3.29, feedparser 6.0, exchange_calendars 4.13 (full list: `backend/requirements.lock`).
Frontend: React 19.3, Vite 6.4, Tailwind CSS 4.3, Recharts 3.10, TanStack Query 5.104, React Router 7.18, TypeScript 5.8, Vitest 3.2 (`frontend/package-lock.json`).
Infra: PostgreSQL 17, Terraform ≥ 1.10, AWS provider ~> 5.80.

## AWS (future)

See [docs/aws-deployment.md](docs/aws-deployment.md) for the architecture, an estimated monthly cost (about $45–65/month for the low-cost dev profile), and the step-by-step deployment. Nothing is deployed until you explicitly run it.
