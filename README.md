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
6. **Quantitative engine** (`app/services/forecasting/engine.py`), run for 3/6/12/24/36/60-month horizons:
   * Revenue growth starts from the average of TTM YoY growth and 3-year CAGR, then fades toward 4%/yr. Bear and bull use ± a spread based on historical growth volatility.
   * Margins are scenario-specific and bounded by history. Share count changes from the dilution/buyback trend.
   * **Valuation regime:** earnings-based (forward P/E 60% + DCF 40%) when net margin ≥ 2%; otherwise sales-based (EV/Sales, plus DCF if FCF is positive). The regime is chosen once per company, so scenarios stay comparable.
   * **P/E band:** company 2-year historical P/E percentiles blended with sector bands (`sectors.py`, labeled as model assumptions). Distorted histories (median > 100x) are ignored.
   * **DCF:** CAPM cost of equity (beta from weekly returns vs SPY, clamped to 0.6–2.0) and WACC bounded to 7.5–16%. Terminal growth is 2–3%, at least 3pp below WACC. DCF is disabled for banks/insurers and for negative FCF, and down-weighted for volatile FCF.
   * Per-share valuation is disabled for foreign 20-F filers, whose ADR ratio is unknown.
   * Outputs: P/E-growth and WACC-terminal-growth sensitivity grids, a separate historical-volatility 1σ band for short-term movement, and **no probabilities**.
7. **Persistence.** Each analysis stores its thesis, sources, snapshot and usage. Each horizon's forecast and scenarios are inserted as new rows. A PostgreSQL trigger blocks UPDATE/DELETE on forecast tables; only the demo reset bypasses it, for demo rows.

**Discovery** ranks candidates with a transparent weighted score, in 5-point steps with a Strong/Moderate/Weak band:
* Positive factors: financial strength (25%), catalyst materiality (20%), valuation (25%), data freshness (10%).
* Penalties: forecast uncertainty (−10%) and downside risk (−10%).

Candidates come from recent and upcoming events plus the watchlist. Illiquid names (20-day average dollar volume < $20M) are skipped. AI is used only for the top N, to control cost.

**Backtesting** (Forecast History page):
1. *Realized*: matured stored forecasts are compared with split-adjusted realized returns and SPY. Tickers that lost price coverage are listed as unresolved, not dropped, to avoid survivorship bias.
2. *Point-in-time replay*: the engine is re-run at past dates using only prices and SEC facts available then, without AI.

### Scheduled jobs (America/New_York)

| Job | Schedule |
|---|---|
| News ingestion | every 60 min |
| Price refresh (one grouped-daily request for the whole universe) | Mon–Fri 16:45, NYSE sessions only |
| SEC filing monitor | Mon–Fri every 2h 07:10–19:10 |
| Catalyst AI interpretation (unchanged events skipped) | Mon–Fri 17:30, sessions only |
| Discovery | Mon–Fri 18:00, sessions only |
| Forecast review (watchlist) | Saturday 09:00 |
| Cache cleanup | daily 03:15 |

The monitored universe (116 large caps) is in `backend/app/data/universe.txt`. Edit it freely.

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

* Free Massive tier: end-of-day prices only (no real-time quotes), about 2 years of history, and 5 requests/minute. A live discovery run over many candidates can take a long time; tune `DISCOVERY_MAX_CANDIDATES`. Index levels need a paid plan, so SPY/QQQ/DIA ETFs are shown as labeled proxies.
* Company-specific press releases depend on the Massive news endpoint. The default RSS feeds are macro/regulatory only. Add investor-relations RSS feeds via `RSS_FEEDS` (comma-separated). They are fetched only if robots.txt allows it.
* Earnings dates are estimates from filing cadence unless a confirmed source is added.
* Event impact and materiality are keyword heuristics unless the AI catalyst job has interpreted the event.
* Sector P/E and EV/Sales bands are heuristic model assumptions, not live market data.
* Foreign issuers (20-F filers, e.g. TSM) show financials, but per-share valuation is disabled (no ADR ratio).
* The rate limiter is per process. The API and worker can each use the Massive quota; on a 429 the client backs off and honors `Retry-After`.
* The UI has no authentication; it is designed for localhost personal use. Add auth before exposing it publicly.

## Dependency versions (resolved Oct 2026)

Backend (Python 3.12): FastAPI 0.143, Pydantic 2.14, SQLAlchemy 2.0.54, Alembic 1.20, psycopg 3.3, pandas 3.0, NumPy 2.5, httpx 0.28, APScheduler 3.11, openai 3.29, feedparser 6.0, exchange_calendars 4.13 (full list: `backend/requirements.lock`).
Frontend: React 19.3, Vite 6.4, Tailwind CSS 4.3, Recharts 3.10, TanStack Query 5.104, React Router 7.18, TypeScript 5.8, Vitest 3.2 (`frontend/package-lock.json`).
Infra: PostgreSQL 17, Terraform ≥ 1.10, AWS provider ~> 5.80.

## AWS (future)

See [docs/aws-deployment.md](docs/aws-deployment.md) for the architecture, an estimated monthly cost (about $45–65/month for the low-cost dev profile), and the step-by-step deployment. Nothing is deployed until you explicitly run it.
