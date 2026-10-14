"""Job handlers executed by the worker."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AnalysisJob, Company, Event, HttpCache
from app.services.ai import AIEngine
from app.services.analysis import analyze_ticker
from app.services.baseline import refresh_baseline
from app.services.discovery import run_discovery
from app.services.filings_index import ingest_filing_indexes
from app.services.http import ProviderError, ProviderNotConfigured
from app.services.market_calendar import last_completed_session, previous_session
from app.services.market_screen import (
    coverage_stats,
    persist_screen,
    refresh_fundamentals,
    refresh_market_bars,
    screen,
)
from app.services.master_universe import make_resolver, refresh_master_universe
from app.services.prices import upsert_prices
from app.services.providers import market_provider
from app.services.screen_coverage import (
    refresh_fundamentals_fallback,
    refresh_market_splits,
    refresh_provider_shares,
)
from app.services.universe import ensure_universe_companies, universe_tickers, watchlist_tickers
from app.services.universe_news import ingest_universe_filings, ingest_universe_news

log = logging.getLogger(__name__)
Progress = Callable[[str], None]


def h_analyze(session: Session, job: AnalysisJob, progress: Progress) -> dict[str, Any]:
    p = job.payload or {}
    out = analyze_ticker(session, p["ticker"], job.data_mode, force=bool(p.get("force")), progress=progress,
                         use_ai=p.get("use_ai", True))
    return {"analysis_id": out.analysis_id, "cached": out.cached, "ticker": p["ticker"].upper(), "warnings": out.warnings}


def _universe(session: Session, mode: str):
    tickers = list(dict.fromkeys(universe_tickers(mode) + watchlist_tickers(session, mode)))
    return ensure_universe_companies(session, mode, tickers)


def _dynamic() -> bool:
    return get_settings().discovery_universe_mode == "dynamic"


def h_ingest_news(session: Session, job: AnalysisJob, progress: Progress) -> dict[str, Any]:
    """Market-wide news (+ rotating per-ticker checks for monitored stocks) + macro RSS. In dynamic
    mode, news can link any listed stock, including ones never monitored."""
    resolver = known = None
    if _dynamic():
        resolver, known = make_resolver(session, job.data_mode)
    return ingest_universe_news(session, job.data_mode, _universe(session, job.data_mode), progress=progress,
                                resolver=resolver, known_tickers=known)


def h_refresh_baseline(session: Session, job: AnalysisJob, progress: Progress) -> dict[str, Any]:
    """Daily market data. Dynamic: market-wide grouped-daily prices + SEC frames + screening.
    Fixed: baseline for the universe.txt stocks."""
    if not _dynamic():
        return refresh_baseline(session, job.data_mode, _universe(session, job.data_mode), progress)
    out = {"prices": refresh_market_bars(session, job.data_mode, progress),
           "fundamentals": refresh_fundamentals(session, job.data_mode, progress)}
    for name, fn in (("splits", lambda: refresh_market_splits(session, job.data_mode)),
                     ("fundamentals_fallback", lambda: refresh_fundamentals_fallback(session, job.data_mode, progress)),
                     ("provider_shares", lambda: refresh_provider_shares(session, job.data_mode, progress))):
        try:  # coverage improvements are optional: never block the daily screen
            out[name] = fn()
        except Exception as exc:
            session.rollback()
            out[name] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    results = screen(session, job.data_mode, progress=progress)
    persist_screen(session, results)
    out["screened"] = len(results)
    out["eligible"] = sum(1 for r in results if r.status in ("eligible", "catalyst_eligible"))
    out["coverage"] = coverage_stats(results)
    return out


def h_refresh_universe(session: Session, job: AnalysisJob, progress: Progress) -> dict[str, Any]:
    """Weekly: master universe of US-listed securities, plus a larger batch of provider share counts
    for securities whose market cap is still unknown (Sunday morning, when nothing else runs)."""
    out = refresh_master_universe(session, job.data_mode, progress)
    out["provider_shares"] = refresh_provider_shares(session, job.data_mode, progress,
                                                     budget=get_settings().provider_shares_per_run * 6)
    return out


def h_refresh_prices(session: Session, job: AnalysisJob, progress: Progress) -> dict[str, Any]:
    """After-close refresh using one grouped-daily request for the whole universe."""
    try:
        prov = market_provider(job.data_mode)
    except ProviderNotConfigured as exc:
        return {"skipped": exc.message}
    day = last_completed_session()
    tickers = list(dict.fromkeys(universe_tickers(job.data_mode) + watchlist_tickers(session, job.data_mode) + ["SPY"]))
    if _dynamic():  # every company that has been analyzed or linked (one grouped request covers all)
        tickers += [t for (t,) in session.execute(select(Company.ticker).where(Company.data_mode == job.data_mode))]
        tickers = list(dict.fromkeys(tickers))
    companies = ensure_universe_companies(session, job.data_mode, tickers)
    if prov.is_demo:
        n = 0
        for t, c in companies.items():
            n += upsert_prices(session, c.id, prov.get_historical_prices(t, day - timedelta(days=10), day), prov.name)
        return {"session": str(day), "rows": n}
    note = None
    try:
        grouped = prov.get_grouped_daily(day)
    except ProviderError as exc:
        if exc.status != 403 or "end of day" not in exc.message:
            raise
        # Free plans release a session's end-of-day data some hours after the close. Keep the previous
        # session (already stored) and let the overnight catch-up job fetch this one.
        note = f"{day} end-of-day data not yet released by the provider plan; using {previous_session(day)}"
        day = previous_session(day)
        grouped = prov.get_grouped_daily(day)
    n = 0
    for t, c in companies.items():
        bar = grouped.get(t) or grouped.get(t.replace("-", "."))
        if bar:
            n += upsert_prices(session, c.id, [bar], prov.name)
    out = {"session": str(day), "rows": n, "missing": [t for t in companies if t not in grouped][:50]}
    if note:
        out["note"] = note
    return out


def h_sec_monitor(session: Session, job: AnalysisJob, progress: Progress) -> dict[str, Any]:
    """SEC filings + estimated earnings dates for monitored companies; in dynamic mode also the
    market-wide EDGAR daily index (every 8-K/10-Q/10-K by any listed company)."""
    out = {"monitored": ingest_universe_filings(session, job.data_mode, _universe(session, job.data_mode),
                                                progress=progress)}
    if _dynamic():
        out["market_wide"] = ingest_filing_indexes(session, job.data_mode, progress)
    return out


def h_catalyst_analysis(session: Session, job: AnalysisJob, progress: Progress) -> dict[str, Any]:
    """AI interpretation of the most material unprocessed events (skips unchanged events)."""
    s = get_settings()
    if job.data_mode == "demo" or not s.openai_configured:
        return {"skipped": "requires live mode and OPENAI_API_KEY"}
    engine = AIEngine(s, session)
    since = datetime.now(UTC) - timedelta(days=3)
    events = list(session.scalars(select(Event).where(
        Event.data_mode == job.data_mode, Event.last_published_at >= since, Event.materiality >= 0.5,
    ).order_by(Event.materiality.desc()).limit(15)))
    done = 0
    for ev in events:
        if ev.ai_processed_hash == ev.content_hash:
            continue
        cands = [{"ticker": rel.company.ticker, "name": rel.company.name, "link_confidence": rel.confidence}
                 for rel in ev.companies]
        sources = [{"id": "S1", "title": ev.title, "publisher": ev.source_name, "url": ev.primary_url,
                    "published_at": str(ev.last_published_at), "excerpt": (ev.summary or "")[:800],
                    "status": ev.status}]
        res, meta = engine.interpret_event({"title": ev.title, "type": ev.event_type, "status": ev.status},
                                           cands, sources)
        if res is None:
            if meta.get("skipped"):
                break
            continue
        ev.ai_analysis = {**res, "_meta": {k: meta.get(k) for k in ("model", "prompt_version", "cost_usd")}}
        ev.ai_processed_hash = ev.content_hash
        if not res.get("is_confirmed") and ev.status == "confirmed":
            ev.status = "unconfirmed"
        done += 1
    return {"events_interpreted": done}


def h_discovery(session: Session, job: AnalysisJob, progress: Progress) -> dict[str, Any]:
    run = run_discovery(session, job.data_mode, progress=progress)
    return {"run_id": run.id, "candidates": run.candidates_evaluated, "notes": run.notes}


def h_forecast_review(session: Session, job: AnalysisJob, progress: Progress) -> dict[str, Any]:
    """Weekly: fresh analysis for watchlist names (new immutable forecasts, history preserved)."""
    out = []
    for t in watchlist_tickers(session, job.data_mode):
        progress(f"Reviewing {t}")
        try:
            res = analyze_ticker(session, t, job.data_mode, force=False)
            session.commit()
            out.append({"ticker": t, "analysis_id": res.analysis_id, "cached": res.cached})
        except (ProviderError, Exception) as exc:  # one failure must not stop the review
            session.rollback()
            out.append({"ticker": t, "error": str(exc)})
    return {"reviewed": out}


def h_cache_cleanup(session: Session, job: AnalysisJob, progress: Progress) -> dict[str, Any]:
    now = datetime.now(UTC)
    n_cache = session.execute(delete(HttpCache).where(HttpCache.expires_at < now)).rowcount
    n_jobs = session.execute(delete(AnalysisJob).where(
        AnalysisJob.finished_at < now - timedelta(days=30), AnalysisJob.dedupe_key.is_(None))).rowcount
    return {"cache_rows_deleted": n_cache, "old_jobs_deleted": n_jobs}


HANDLERS: dict[str, Callable[[Session, AnalysisJob, Progress], dict[str, Any]]] = {
    "analyze_ticker": h_analyze,
    "ingest_news": h_ingest_news,
    "refresh_baseline": h_refresh_baseline,
    "refresh_universe": h_refresh_universe,
    "refresh_prices": h_refresh_prices,
    "sec_monitor": h_sec_monitor,
    "catalyst_analysis": h_catalyst_analysis,
    "discovery": h_discovery,
    "forecast_review": h_forecast_review,
    "cache_cleanup": h_cache_cleanup,
}

