"""Remaining API routes: health, market, discovery, events, watchlist, jobs, settings, backtest, demo."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import delete, or_, select, text
from sqlalchemy.orm import Session

from app.api.common import (
    MODEL_FORECAST,
    OBSERVED,
    analysis_summary,
    api_error,
    current_mode,
    diff_snapshots,
    forecast_dict,
    iso,
    latest_price,
)
from app.api.stocks import event_dict
from app.config import get_settings
from app.db import get_db
from app.models import (
    Analysis,
    AnalysisJob,
    Company,
    DiscoveryRun,
    Event,
    EventCompanyRelationship,
    Forecast,
    NewsArticle,
    Recommendation,
    WatchlistItem,
)
from app.services import jobs
from app.services.ai import month_spend
from app.services.analysis import AnalysisError, get_or_create_company, normalize_ticker
from app.services.backtest import point_in_time_replay, realized_evaluation
from app.services.discovery import RANKING_WEIGHTS
from app.services.events import upcoming_for_company
from app.services.forecasting.engine import HORIZONS_MONTHS, MODEL_VERSION
from app.services.http import ProviderError, ProviderNotConfigured
from app.services.providers import market_provider, set_data_mode

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------------- health
@router.get("/health", tags=["system"], summary="Liveness/readiness")
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    s = get_settings()
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    from app.services.providers import get_data_mode

    return {
        "status": "ok" if db_ok else "degraded", "database": "ok" if db_ok else "unreachable",
        "version": s.api_version, "data_mode": get_data_mode(db) if db_ok else s.data_mode,
        "providers": {"massive_configured": s.massive_configured, "openai_configured": s.openai_configured,
                      "sec_user_agent_has_contact": "@" in s.sec_user_agent and "example.com" not in s.sec_user_agent},
        "time": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------------- market
@router.get("/market/overview", tags=["market"], summary="Market index overview")
def market_overview(mode: str = Depends(current_mode)) -> dict[str, Any]:
    try:
        prov = market_provider(mode)
    except ProviderNotConfigured as exc:
        return {"data_mode": mode, "indices": [], "unavailable_reason": exc.message, "coverage_notes": []}
    try:
        quotes = prov.get_market_indices()
    except ProviderError as exc:
        return {"data_mode": mode, "indices": [], "unavailable_reason": exc.message, "coverage_notes": prov.coverage_notes}
    return {"data_mode": mode, "provenance": OBSERVED, "indices": [q.model_dump(mode="json") for q in quotes],
            "coverage_notes": prov.coverage_notes}


# ---------------------------------------------------------------------------------- discovery
@router.get("/discovery", tags=["discovery"], summary="Latest ranked opportunities")
def discovery(
    sector: str | None = None, risk: str | None = None, horizon: str | None = None,
    sort: Literal["score", "growth", "risk", "event_date", "forecast_date"] = "score",
    db: Session = Depends(get_db), mode: str = Depends(current_mode),
) -> dict[str, Any]:
    run = db.scalar(select(DiscoveryRun).where(DiscoveryRun.data_mode == mode, DiscoveryRun.status == "succeeded")
                    .order_by(DiscoveryRun.started_at.desc()))
    running = db.scalar(select(AnalysisJob).where(AnalysisJob.job_type == "discovery", AnalysisJob.data_mode == mode,
                                                  AnalysisJob.status.in_(("queued", "running"))))
    if run is None:
        return {"data_mode": mode, "run": None, "items": [], "running_job": _job_dict(running) if running else None}
    items = []
    for r in db.scalars(select(Recommendation).where(Recommendation.run_id == run.id).order_by(Recommendation.rank)):
        c = r.company
        a = db.get(Analysis, r.analysis_id) if r.analysis_id else None
        f = db.get(Forecast, r.forecast_id) if r.forecast_id else None
        base = next((s for s in f.scenarios if s.scenario == "base"), None) if f else None
        items.append({
            "rank": r.rank, "ticker": c.ticker, "name": c.name, "sector": c.sector,
            "latest_price": latest_price(db, c.id), "score": r.score, "score_band": r.score_band, "factors": r.factors,
            "outlook": r.outlook, "risk_rating": r.risk_rating, "horizon_class": r.horizon_class,
            "potential_return": base.potential_return if base else None,
            "target_price": base.target_price if base else None, "target_date": iso(f.target_date) if f else None,
            "forecast_provenance": MODEL_FORECAST,
            "bear_target": next((s.target_price for s in f.scenarios if s.scenario == "bear"), None) if f else None,
            "bull_target": next((s.target_price for s in f.scenarios if s.scenario == "bull"), None) if f else None,
            "catalyst": {"title": r.catalyst_title, "event_id": r.catalyst_event_id, "event_date": iso(r.catalyst_date)},
            "analysis_id": r.analysis_id, "analysis_at": iso(a.created_at) if a else None,
            "ai_kind": a.ai_kind if a else None,
        })
    if sector:
        items = [i for i in items if (i["sector"] or "").lower() == sector.lower()]
    if risk:
        items = [i for i in items if i["risk_rating"].lower() == risk.lower()]
    if horizon:
        items = [i for i in items if i["horizon_class"] == horizon]
    risk_order = {"Low": 0, "Medium": 1, "High": 2}
    keyf = {
        "score": lambda i: -i["score"],
        "growth": lambda i: -(i["potential_return"] if i["potential_return"] is not None else -9),
        "risk": lambda i: risk_order.get(i["risk_rating"], 3),
        "event_date": lambda i: i["catalyst"]["event_date"] or "9999",
        "forecast_date": lambda i: i["target_date"] or "9999",
    }[sort]
    items.sort(key=keyf)
    return {
        "data_mode": mode, "items": items, "running_job": _job_dict(running) if running else None,
        "run": {"id": run.id, "started_at": iso(run.started_at), "finished_at": iso(run.finished_at),
                "universe_size": run.universe_size, "candidates_evaluated": run.candidates_evaluated,
                "config": run.config, "notes": run.notes},
        "ranking_explanation": (
            "Score = weighted sum of financial strength, catalyst materiality, valuation (base-case 12m return), "
            "data freshness, minus forecast uncertainty and downside risk; rounded to 5 points. Upside alone does "
            "not determine rank."),
    }


@router.post("/discovery/run", tags=["discovery"], status_code=202, summary="Start a discovery run")
def discovery_run(db: Session = Depends(get_db), mode: str = Depends(current_mode)) -> dict[str, Any]:
    job, created = jobs.enqueue(db, "discovery", mode, {}, dedupe_key=f"discovery-manual:{mode}")
    return {"job_id": str(job.id), "status": job.status, "created": created}


# ---------------------------------------------------------------------------------- events
@router.get("/events", tags=["events"], summary="Event radar")
def events(
    event_type: str | None = None, impact: str | None = None, status: str | None = None,
    upcoming: bool | None = None, days: int = Query(7, ge=1, le=90), limit: int = Query(100, le=500),
    db: Session = Depends(get_db), mode: str = Depends(current_mode),
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    q = select(Event).where(Event.data_mode == mode)
    if upcoming is True:
        q = q.where(Event.event_date >= date.today()).order_by(Event.event_date)
    elif upcoming is False:
        q = q.where(Event.event_date.is_(None), Event.last_published_at >= since).order_by(Event.last_published_at.desc())
    else:
        q = q.where(or_(Event.last_published_at >= since, Event.event_date >= date.today())).order_by(
            Event.last_published_at.desc())
    if event_type:
        q = q.where(Event.event_type == event_type)
    if impact:
        q = q.where(Event.impact == impact)
    if status:
        q = q.where(Event.status == status)
    out = []
    for ev in db.scalars(q.limit(limit)):
        d = event_dict(ev, None, list(db.scalars(select(NewsArticle).where(NewsArticle.event_id == ev.id).limit(10))))
        d["companies"] = [{"ticker": rel.company.ticker, "name": rel.company.name, "confidence": rel.confidence,
                           "method": rel.method, "impact": rel.impact} for rel in ev.companies]
        out.append(d)
    types = [r[0] for r in db.execute(select(Event.event_type).where(Event.data_mode == mode).distinct())]
    return {"data_mode": mode, "events": out, "event_types": sorted(types),
            "notes": ["Articles covering the same story are merged into one event.",
                      "Events marked 'unconfirmed' rely on unnamed sources or speculative language and are not facts.",
                      "Impact labels are keyword heuristics unless an AI interpretation is attached."]}


@router.post("/events/ingest", tags=["events"], status_code=202, summary="Trigger news ingestion now")
def events_ingest(db: Session = Depends(get_db), mode: str = Depends(current_mode)) -> dict[str, Any]:
    job, created = jobs.enqueue(db, "ingest_news", mode, {}, dedupe_key=f"ingest-manual:{mode}")
    return {"job_id": str(job.id), "status": job.status, "created": created}


# ---------------------------------------------------------------------------------- watchlist
class WatchlistAdd(BaseModel):
    ticker: str
    notes: str | None = None


@router.get("/watchlist", tags=["watchlist"])
def watchlist(db: Session = Depends(get_db), mode: str = Depends(current_mode)) -> dict[str, Any]:
    out = []
    rows = db.scalars(select(WatchlistItem).join(Company).where(Company.data_mode == mode).order_by(WatchlistItem.added_at))
    for w in rows:
        c = w.company
        analyses = db.scalars(select(Analysis).where(Analysis.company_id == c.id).order_by(Analysis.created_at.desc()).limit(2)).all()
        cur = analyses[0] if analyses else None
        prev = analyses[1] if len(analyses) > 1 else None
        hist = db.scalars(select(Forecast).where(Forecast.company_id == c.id, Forecast.horizon_months == 12)
                          .order_by(Forecast.created_at)).all()
        out.append({
            "ticker": c.ticker, "name": c.name, "sector": c.sector, "added_at": iso(w.added_at), "notes": w.notes,
            "latest_price": latest_price(db, c.id), "latest_analysis": analysis_summary(cur) if cur else None,
            "previous_analysis": analysis_summary(prev) if prev else None,
            "diff": diff_snapshots(cur.snapshot or {}, prev.snapshot or {}) if cur and prev else None,
            "forecast_history_12m": [{"created_at": iso(f.created_at), "observed_price": f.observed_price,
                                      **{s.scenario: s.target_price for s in f.scenarios}} for f in hist],
            "upcoming": [{"title": e.title, "event_date": iso(e.event_date), "status": e.status, "event_type": e.event_type}
                         for e in upcoming_for_company(db, c.id)[:5]],
        })
    return {"data_mode": mode, "items": out}


@router.post("/watchlist", tags=["watchlist"], status_code=201)
def watchlist_add(body: WatchlistAdd, db: Session = Depends(get_db), mode: str = Depends(current_mode)) -> dict[str, Any]:
    try:
        t = normalize_ticker(body.ticker)
    except AnalysisError as exc:
        raise api_error(400, exc.code, str(exc)) from exc
    c = get_or_create_company(db, t, mode)
    if db.scalar(select(WatchlistItem).where(WatchlistItem.company_id == c.id)):
        return {"ticker": t, "added": False, "message": "already on watchlist"}
    db.add(WatchlistItem(company_id=c.id, notes=body.notes))
    job_id = None
    if not db.scalar(select(Analysis.id).where(Analysis.company_id == c.id).limit(1)):
        job, _ = jobs.enqueue(db, "analyze_ticker", mode, {"ticker": t}, dedupe_key=f"analyze:{mode}:{t}")
        job_id = str(job.id)
    return {"ticker": t, "added": True, "analysis_job_id": job_id}


@router.delete("/watchlist/{ticker}", tags=["watchlist"])
def watchlist_remove(ticker: str, db: Session = Depends(get_db), mode: str = Depends(current_mode)) -> dict[str, Any]:
    c = db.scalar(select(Company).where(Company.ticker == ticker.upper(), Company.data_mode == mode))
    if c is None:
        raise api_error(404, "not_found", f"{ticker.upper()} is not on the watchlist")
    n = db.execute(delete(WatchlistItem).where(WatchlistItem.company_id == c.id)).rowcount
    if not n:
        raise api_error(404, "not_found", f"{ticker.upper()} is not on the watchlist")
    return {"ticker": c.ticker, "removed": True}


# ---------------------------------------------------------------------------------- jobs
def _job_dict(j: AnalysisJob) -> dict[str, Any]:
    return {"id": str(j.id), "job_type": j.job_type, "status": j.status, "data_mode": j.data_mode,
            "payload": {k: v for k, v in (j.payload or {}).items() if not k.startswith("_")},
            "progress": j.progress, "result": j.result, "error": j.error, "attempts": j.attempts,
            "created_at": iso(j.created_at), "started_at": iso(j.started_at), "finished_at": iso(j.finished_at)}


@router.get("/jobs/{job_id}", tags=["jobs"])
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    j = db.get(AnalysisJob, job_id)
    if j is None:
        raise api_error(404, "not_found", "job not found")
    return _job_dict(j)


@router.get("/jobs", tags=["jobs"])
def list_jobs(limit: int = Query(25, le=200), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"jobs": [_job_dict(j) for j in db.scalars(select(AnalysisJob).order_by(AnalysisJob.created_at.desc()).limit(limit))]}


# ---------------------------------------------------------------------------------- settings
class SettingsUpdate(BaseModel):
    data_mode: Literal["demo", "live"]


@router.get("/settings", tags=["system"])
def get_settings_route(db: Session = Depends(get_db), mode: str = Depends(current_mode)) -> dict[str, Any]:
    s = get_settings()
    spent = month_spend(db)
    return {
        "data_mode": mode, "default_data_mode": s.data_mode,
        "providers": {
            "market_data": {"name": "Massive (formerly Polygon.io)", "configured": s.massive_configured,
                            "base_url": s.massive_base_url, "requests_per_minute": s.massive_requests_per_minute},
            "sec_edgar": {"user_agent": s.sec_user_agent, "requests_per_second": s.sec_requests_per_second,
                          "has_contact": "@" in s.sec_user_agent and "example.com" not in s.sec_user_agent},
            "openai": {"configured": s.openai_configured, "model": s.openai_model},
            "rss_feeds": s.rss_feeds,
        },
        "ai_budget": {"monthly_budget_usd": s.ai_monthly_budget_usd, "spent_this_month_usd": round(spent, 4),
                      "remaining_usd": round(max(0.0, s.ai_monthly_budget_usd - spent), 4),
                      "cache_ttl_hours": s.ai_cache_ttl_hours,
                      "pricing_per_million": {"input": s.openai_input_price_per_m, "output": s.openai_output_price_per_m}},
        "forecasting": {"model_version": MODEL_VERSION, "horizons_months": list(HORIZONS_MONTHS),
                        "risk_free_rate": s.risk_free_rate, "equity_risk_premium": s.equity_risk_premium,
                        "pre_tax_cost_of_debt": s.pre_tax_cost_of_debt, "tax_rate": s.tax_rate},
        "discovery": {"weights": RANKING_WEIGHTS, "top_n": s.discovery_top_n, "max_candidates": s.discovery_max_candidates,
                      "ai_top_n": s.discovery_ai_top_n, "min_avg_dollar_volume": s.min_avg_dollar_volume},
        "scheduler": {"enabled": s.scheduler_enabled, "timezone": "America/New_York",
                      "jobs": [
                          {"id": "ingest_news", "schedule": f"every {s.news_ingest_interval_minutes} min"},
                          {"id": "refresh_prices", "schedule": "Mon–Fri 16:45 ET on NYSE sessions"},
                          {"id": "sec_monitor", "schedule": "Mon–Fri every 2h 07:10–19:10 ET"},
                          {"id": "catalyst_analysis", "schedule": "Mon–Fri 17:30 ET on NYSE sessions"},
                          {"id": "discovery", "schedule": "Mon–Fri 18:00 ET on NYSE sessions"},
                          {"id": "forecast_review", "schedule": "Saturday 09:00 ET"},
                          {"id": "cache_cleanup", "schedule": "daily 03:15 ET"}]},
    }


@router.put("/settings", tags=["system"])
def update_settings(body: SettingsUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    set_data_mode(db, body.data_mode)
    return {"data_mode": body.data_mode}


# ---------------------------------------------------------------------------------- backtest
@router.get("/backtest", tags=["forecasts"], summary="Realized accuracy of stored forecasts vs S&P 500")
def backtest(horizon: int | None = None, db: Session = Depends(get_db), mode: str = Depends(current_mode)) -> dict[str, Any]:
    return {"data_mode": mode, **realized_evaluation(db, mode, horizon)}


@router.get("/backtest/replay/{ticker}", tags=["forecasts"], summary="Point-in-time replay backtest")
def backtest_replay(ticker: str, horizon: int = Query(6, ge=3, le=24), db: Session = Depends(get_db),
                    mode: str = Depends(current_mode)) -> dict[str, Any]:
    return {"data_mode": mode, **point_in_time_replay(db, ticker.upper(), mode, horizon)}


@router.get("/forecasts", tags=["forecasts"], summary="Recent forecasts across all tickers")
def recent_forecasts(horizon: int = 12, limit: int = Query(100, le=500), db: Session = Depends(get_db),
                     mode: str = Depends(current_mode)) -> dict[str, Any]:
    rows = db.execute(select(Forecast, Company).join(Company, Company.id == Forecast.company_id).where(
        Forecast.data_mode == mode, Forecast.horizon_months == horizon).order_by(Forecast.created_at.desc()).limit(limit)).all()
    return {"data_mode": mode, "forecasts": [{"ticker": c.ticker, "name": c.name, **forecast_dict(f, include_detail=False)}
                                             for f, c in rows]}


# ---------------------------------------------------------------------------------- demo
@router.post("/demo/reset", tags=["system"], summary="Delete all DEMO-mode data")
def demo_reset(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Removes demo rows only. Forecast tables are append-only (trigger-protected), so demo
    forecasts are removed via a session-scoped bypass flag honored by the trigger."""
    db.execute(text("SET LOCAL eventalpha.allow_forecast_delete = 'on'"))
    demo_ids = select(Company.id).where(Company.data_mode == "demo")
    db.execute(delete(Recommendation).where(Recommendation.company_id.in_(demo_ids)))
    db.execute(delete(DiscoveryRun).where(DiscoveryRun.data_mode == "demo"))
    fids = select(Forecast.id).where(Forecast.data_mode == "demo")
    db.execute(text("DELETE FROM forecast_scenarios WHERE forecast_id IN (SELECT id FROM forecasts WHERE data_mode='demo')"))
    db.execute(delete(Forecast).where(Forecast.id.in_(fids)))
    db.execute(delete(Analysis).where(Analysis.data_mode == "demo"))
    db.execute(delete(NewsArticle).where(NewsArticle.data_mode == "demo"))
    db.execute(delete(EventCompanyRelationship).where(EventCompanyRelationship.company_id.in_(demo_ids)))
    db.execute(delete(Event).where(Event.data_mode == "demo"))
    n = db.execute(delete(Company).where(Company.data_mode == "demo")).rowcount
    db.execute(text("DELETE FROM source_references WHERE data_mode='demo'"))
    db.execute(delete(AnalysisJob).where(AnalysisJob.data_mode == "demo"))
    return {"reset": True, "companies_removed": n}


