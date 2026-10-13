"""Job handlers executed by the worker."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AnalysisJob, Event, HttpCache
from app.services.ai import AIEngine
from app.services.analysis import analyze_ticker
from app.services.discovery import ingest_universe_news, run_discovery
from app.services.http import ProviderError, ProviderNotConfigured
from app.services.market_calendar import last_completed_session
from app.services.prices import upsert_prices
from app.services.providers import market_provider
from app.services.universe import ensure_universe_companies, universe_tickers, watchlist_tickers

log = logging.getLogger(__name__)
Progress = Callable[[str], None]


def h_analyze(session: Session, job: AnalysisJob, progress: Progress) -> dict[str, Any]:
    p = job.payload or {}
    out = analyze_ticker(session, p["ticker"], job.data_mode, force=bool(p.get("force")), progress=progress,
                         use_ai=p.get("use_ai", True))
    return {"analysis_id": out.analysis_id, "cached": out.cached, "ticker": p["ticker"].upper(), "warnings": out.warnings}


def h_ingest_news(session: Session, job: AnalysisJob, progress: Progress) -> dict[str, Any]:
    tickers = list(dict.fromkeys(universe_tickers(job.data_mode) + watchlist_tickers(session, job.data_mode)))
    companies = ensure_universe_companies(session, job.data_mode, tickers)
    return ingest_universe_news(session, job.data_mode, companies)


def h_refresh_prices(session: Session, job: AnalysisJob, progress: Progress) -> dict[str, Any]:
    """After-close refresh using one grouped-daily request for the whole universe."""
    try:
        prov = market_provider(job.data_mode)
    except ProviderNotConfigured as exc:
        return {"skipped": exc.message}
    day = last_completed_session()
    tickers = list(dict.fromkeys(universe_tickers(job.data_mode) + watchlist_tickers(session, job.data_mode) + ["SPY"]))
    companies = ensure_universe_companies(session, job.data_mode, tickers)
    if prov.is_demo:
        n = 0
        for t, c in companies.items():
            n += upsert_prices(session, c.id, prov.get_historical_prices(t, day - timedelta(days=10), day), prov.name)
        return {"session": str(day), "rows": n}
    grouped = prov.get_grouped_daily(day)
    n = 0
    for t, c in companies.items():
        bar = grouped.get(t) or grouped.get(t.replace("-", "."))
        if bar:
            n += upsert_prices(session, c.id, [bar], prov.name)
    return {"session": str(day), "rows": n, "missing": [t for t in companies if t not in grouped][:50]}


def h_sec_monitor(session: Session, job: AnalysisJob, progress: Progress) -> dict[str, Any]:
    from app.services.events import ingest_articles
    from app.services.providers import news_provider

    tickers = list(dict.fromkeys(watchlist_tickers(session, job.data_mode) + universe_tickers(job.data_mode)[:40]))
    companies = ensure_universe_companies(session, job.data_mode, tickers)
    prov = news_provider(job.data_mode, tickers)
    total: dict[str, int] = {}
    for t in tickers:
        progress(f"SEC filings {t}")
        stats = ingest_articles(session, job.data_mode, prov.fetch_sec_filings(t), {t: companies[t]})
        for k, v in stats.items():
            total[k] = total.get(k, 0) + v
    return total


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
    "refresh_prices": h_refresh_prices,
    "sec_monitor": h_sec_monitor,
    "catalyst_analysis": h_catalyst_analysis,
    "discovery": h_discovery,
    "forecast_review": h_forecast_review,
    "cache_cleanup": h_cache_cleanup,
}

