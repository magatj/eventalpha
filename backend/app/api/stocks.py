"""/api/stocks/* routes."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.common import (
    OBSERVED,
    analysis_summary,
    api_error,
    current_mode,
    diff_snapshots,
    forecast_dict,
    iso,
    latest_price,
    require_company,
)
from app.config import get_settings
from app.db import get_db
from app.models import (
    Analysis,
    AnalysisSource,
    Company,
    Event,
    EventCompanyRelationship,
    Forecast,
    NewsArticle,
)
from app.services import jobs
from app.services.analysis import AnalysisError, get_company, normalize_ticker
from app.services.financials import build_snapshot, load_facts
from app.services.http import ProviderError
from app.services.prices import load_adjusted_prices, price_stats

router = APIRouter(prefix="/api/stocks", tags=["stocks"])

RANGES = {"1m": 31, "3m": 92, "6m": 183, "1y": 366, "2y": 731, "5y": 1827, "max": 10000}


@router.get("/search", summary="Search tickers/companies")
def search(q: str = Query(..., min_length=1, max_length=40), db: Session = Depends(get_db),
           mode: str = Depends(current_mode)) -> dict[str, Any]:
    ql = q.strip().upper()
    rows = db.scalars(select(Company).where(Company.data_mode == mode, or_(
        Company.ticker.startswith(ql), Company.name.ilike(f"%{q.strip()}%"))).limit(10)).all()
    results = [{"ticker": c.ticker, "name": c.name, "exchange": c.exchange, "analyzed": True} for c in rows]
    if mode == "live" and len(results) < 10:
        try:
            from app.services.providers import edgar_client

            m = edgar_client().ticker_map()
            seen = {r["ticker"] for r in results}
            for t, ident in m.items():
                if len(results) >= 10:
                    break
                if t not in seen and (t.startswith(ql) or q.strip().lower() in ident.name.lower()):
                    results.append({"ticker": t, "name": ident.name, "exchange": ident.exchange, "analyzed": False})
        except ProviderError:
            pass
    return {"data_mode": mode, "results": results}


@router.get("/{ticker}", summary="Stock overview")
def stock_overview(ticker: str, db: Session = Depends(get_db), mode: str = Depends(current_mode)) -> dict[str, Any]:
    c = require_company(db, ticker, mode)
    px = latest_price(db, c.id)
    df = load_adjusted_prices(db, c.id, start=date.today() - timedelta(days=400))
    stats = price_stats(df)
    a = db.scalar(select(Analysis).where(Analysis.company_id == c.id).order_by(Analysis.created_at.desc()))
    snap = (a.snapshot if a else None) or {}
    eps = snap.get("eps_ttm")
    shares = c.shares_outstanding
    mcap = px["price"] * shares if px and shares else c.market_cap
    basis = "latest close × provider shares outstanding" if px and shares else "provider reported"
    vmc = ((a.valuation or {}).get("market_cap") or {}) if a else {}
    if vmc.get("shares") and px:  # SEC current shares outstanding (split-adjusted) from the latest analysis
        mcap, basis = px["price"] * vmc["shares"], "latest close × SEC shares outstanding"
    return {
        "data_mode": mode, "ticker": c.ticker, "name": c.name, "exchange": c.exchange, "cik": c.cik,
        "sector": c.sector, "industry": c.industry, "fiscal_year_end": c.fiscal_year_end,
        "latest_price": px,
        "market_cap": {"value": mcap, "provenance": OBSERVED, "basis": basis},
        "high_52w": stats.get("high_52w"), "low_52w": stats.get("low_52w"),
        "avg_dollar_volume_20d": stats.get("avg_dollar_volume_20d"), "volatility_1y": stats.get("volatility_1y"),
        "return_1y": stats.get("return_1y"),
        "pe_ttm": (px["price"] / eps) if px and eps and eps > 0 else None,
        "pe_note": None if eps and eps > 0 else "P/E not meaningful (TTM EPS ≤ 0 or unavailable)",
        "latest_analysis": analysis_summary(a) if a else None,
    }


@router.get("/{ticker}/prices", summary="Split-adjusted daily prices")
def stock_prices(ticker: str, range: str = Query("1y", pattern="^(1m|3m|6m|1y|2y|5y|max)$"),
                 db: Session = Depends(get_db), mode: str = Depends(current_mode)) -> dict[str, Any]:
    c = require_company(db, ticker, mode)
    df = load_adjusted_prices(db, c.id, start=date.today() - timedelta(days=RANGES[range]))
    from app.services.analysis import BENCHMARK

    bench = get_company(db, BENCHMARK, mode)
    bdf = load_adjusted_prices(db, bench.id, start=date.today() - timedelta(days=RANGES[range])) if bench else None
    bars = [{"date": str(i.date()), "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume}
            for i, r in df.iterrows()]
    return {
        "data_mode": mode, "ticker": c.ticker, "adjusted": "split-adjusted", "provenance": OBSERVED, "bars": bars,
        "benchmark": {"ticker": BENCHMARK, "bars": [{"date": str(i.date()), "close": r.close} for i, r in bdf.iterrows()]}
        if bdf is not None and not bdf.empty else None,
    }


def _series(points: list, n: int) -> list[dict[str, Any]]:
    return [{"period_start": iso(p.period_start), "period_end": str(p.period_end), "value": p.value,
             "fiscal_year": p.fiscal_year, "fiscal_period": p.fiscal_period, "accession": p.accession,
             "filed_date": str(p.filed_date), "derived": p.derived, "concept": p.concept} for p in points[-n:]]


@router.get("/{ticker}/financials", summary="Normalized SEC financials")
def stock_financials(ticker: str, db: Session = Depends(get_db), mode: str = Depends(current_mode)) -> dict[str, Any]:
    c = require_company(db, ticker, mode)
    facts = load_facts(db, c.id)
    if not facts:
        raise api_error(404, "no_financials", f"No financial statements stored for {c.ticker}.")
    snap = build_snapshot(facts)
    metrics = ["revenue", "gross_profit", "operating_income", "net_income", "eps_diluted", "operating_cash_flow",
               "capex", "diluted_shares"]
    quarterly = {m: _series(snap.quarterly.get(m, []), 12) for m in metrics}
    annual = {m: _series(snap.annual.get(m, []), 6) for m in metrics}
    # derived FCF series (OCF - capex) per period where both exist
    for bucket, src in ((quarterly, snap.quarterly), (annual, snap.annual)):
        ocf = {p.period_end: p for p in src.get("operating_cash_flow", [])}
        cx = {p.period_end: p for p in src.get("capex", [])}
        bucket["free_cash_flow"] = [{"period_end": str(e), "value": ocf[e].value - abs(cx[e].value),
                                     "fiscal_year": ocf[e].fiscal_year, "fiscal_period": ocf[e].fiscal_period,
                                     "derived": True} for e in sorted(ocf) if e in cx][-12:]
    accn_urls = {}
    if c.cik:
        for f in facts:
            if f.accession not in accn_urls and not f.accession.startswith("DEMO"):
                accn_urls[f.accession] = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={c.cik}"
                                          f"&type={f.form or ''}&dateb=&owner=include&count=40")
    return {
        "data_mode": mode, "ticker": c.ticker, "currency": snap.currency, "provenance": OBSERVED,
        "source": "demo_synthetic" if mode == "demo" else "SEC EDGAR XBRL CompanyFacts",
        "source_url": None if mode == "demo" or not c.cik else f"https://data.sec.gov/api/xbrl/companyfacts/CIK{c.cik}.json",
        "financial_data_version": snap.financial_data_version, "latest_filed": iso(snap.latest_filed),
        "ttm_period_end": iso(snap.ttm_period_end), "ttm": snap.ttm, "ttm_prior": snap.ttm_prior,
        "ratios": snap.ratios, "margin_history": snap.margin_history,
        "balance_sheet": {k: {"value": v.value, "as_of": str(v.period_end), "accession": v.accession,
                              "filed_date": str(v.filed_date)} for k, v in snap.instant.items()},
        "quarterly": quarterly, "annual": annual, "warnings": snap.warnings,
        "notes": ["Quarterly cash-flow values marked 'derived' were computed from year-to-date filings "
                  "(e.g. Q4 = fiscal year − nine months); they are not reported directly."],
    }


@router.get("/{ticker}/news", summary="Company news, filings and upcoming catalysts")
def stock_news(ticker: str, days: int = Query(30, ge=1, le=365), db: Session = Depends(get_db),
               mode: str = Depends(current_mode)) -> dict[str, Any]:
    c = require_company(db, ticker, mode)
    since = datetime.now(UTC) - timedelta(days=days)
    rows = db.execute(select(Event, EventCompanyRelationship).join(EventCompanyRelationship).where(
        EventCompanyRelationship.company_id == c.id,
        or_(Event.last_published_at >= since, Event.event_date >= date.today()),
    ).order_by(Event.last_published_at.desc())).all()
    out = []
    for ev, link in rows:
        arts = db.scalars(select(NewsArticle).where(NewsArticle.event_id == ev.id).order_by(NewsArticle.published_at)).all()
        out.append(event_dict(ev, link, arts))
    return {"data_mode": mode, "ticker": c.ticker,
            "recent": [e for e in out if not e["event_date"]],
            "upcoming": sorted([e for e in out if e["event_date"]], key=lambda e: e["event_date"])}


def event_dict(ev: Event, link: EventCompanyRelationship | None, arts: list[NewsArticle]) -> dict[str, Any]:
    return {
        "id": ev.id, "title": ev.title, "summary": ev.summary, "event_type": ev.event_type, "status": ev.status,
        "impact": link.impact if link else ev.impact, "impact_explanation": ev.impact_explanation,
        "materiality": ev.materiality, "source_name": ev.source_name, "url": ev.primary_url,
        "published_at": iso(ev.first_published_at), "last_published_at": iso(ev.last_published_at),
        "event_date": iso(ev.event_date), "article_count": ev.article_count,
        "relationship_confidence": link.confidence if link else None, "link_method": link.method if link else None,
        "ai_analysis": ev.ai_analysis,
        "articles": [{"title": a.title, "url": a.url, "source_name": a.source_name, "published_at": iso(a.published_at),
                      "is_rumor": a.is_rumor, "provider": a.provider} for a in arts],
    }


class AnalyzeRequest(BaseModel):
    force: bool = False
    use_ai: bool = True


@router.post("/{ticker}/analyze", status_code=202, summary="Start (or reuse) an analysis job")
def analyze(ticker: str, body: AnalyzeRequest | None = None, db: Session = Depends(get_db),
            mode: str = Depends(current_mode)):
    body = body or AnalyzeRequest()
    try:
        t = normalize_ticker(ticker)
    except AnalysisError as exc:
        raise api_error(400, exc.code, str(exc)) from exc
    if not body.force:
        c = get_company(db, t, mode)
        if c:
            ttl = timedelta(hours=get_settings().analysis_cache_ttl_hours)
            a = db.scalar(select(Analysis).where(Analysis.company_id == c.id,
                                                 Analysis.created_at >= datetime.now(UTC) - ttl)
                          .order_by(Analysis.created_at.desc()))
            if a and (a.ai_kind != "rule_based" or not body.use_ai or not get_settings().openai_configured):
                return JSONResponse(status_code=200, content={
                    "status": "cached", "analysis_id": a.id, "ticker": t, "created_at": a.created_at.isoformat(),
                    "message": "A fresh analysis exists; pass force=true to run a new one."})
    job, created = jobs.enqueue(db, "analyze_ticker", mode, {"ticker": t, "force": body.force, "use_ai": body.use_ai},
                                dedupe_key=f"analyze:{mode}:{t}")
    return {"status": job.status, "job_id": str(job.id), "ticker": t, "created": created}


@router.get("/{ticker}/analysis", summary="Latest (or specific) analysis with thesis and sources")
def get_analysis(ticker: str, analysis_id: int | None = None, db: Session = Depends(get_db),
                 mode: str = Depends(current_mode)) -> dict[str, Any]:
    c = require_company(db, ticker, mode)
    q = select(Analysis).where(Analysis.company_id == c.id)
    if analysis_id:
        q = q.where(Analysis.id == analysis_id)
    a = db.scalar(q.order_by(Analysis.created_at.desc()))
    if a is None:
        raise api_error(404, "no_analysis", f"No analysis for {c.ticker} yet.")
    prev = db.scalar(select(Analysis).where(Analysis.company_id == c.id, Analysis.created_at < a.created_at)
                     .order_by(Analysis.created_at.desc()))
    srcs = db.scalars(select(AnalysisSource).where(AnalysisSource.analysis_id == a.id)).all()
    return {
        "data_mode": mode, "ticker": c.ticker, "company": c.name, **analysis_summary(a),
        "thesis": a.thesis, "snapshot": a.snapshot, "warnings": a.warnings or [], "valuation": a.valuation,
        "prompt_version": a.prompt_version, "usage": a.usage, "cost_usd": a.cost_usd,
        "financial_data_version": a.financial_data_version,
        "sources": sorted([{"label": s.label, "type": s.source.source_type, "title": s.source.title, "url": s.source.url,
                            "publisher": s.source.publisher, "published_at": iso(s.source.published_at)}
                           for s in srcs], key=lambda x: int(x["label"][1:])),
        "previous": analysis_summary(prev) if prev else None,
        "diff": diff_snapshots(a.snapshot or {}, prev.snapshot or {}) if prev else None,
        "disclaimer": "Research tool output, not investment advice. Forecasts are model estimates.",
    }


@router.get("/{ticker}/forecast", summary="Forecasts (all horizons) from the latest analysis")
def get_forecast(ticker: str, analysis_id: int | None = None, db: Session = Depends(get_db),
                 mode: str = Depends(current_mode)) -> dict[str, Any]:
    c = require_company(db, ticker, mode)
    if analysis_id is None:
        analysis_id = db.scalar(select(Analysis.id).where(Analysis.company_id == c.id).order_by(Analysis.created_at.desc()))
    if analysis_id is None:
        raise api_error(404, "no_forecast", f"No forecast for {c.ticker} yet.")
    fs = db.scalars(select(Forecast).where(Forecast.analysis_id == analysis_id).order_by(Forecast.horizon_months)).all()
    return {"data_mode": mode, "ticker": c.ticker, "analysis_id": analysis_id,
            "forecasts": [forecast_dict(f) for f in fs],
            "interpretation": ("Targets are fundamental value estimates under each scenario at the horizon date. "
                               "They are not predictions that the price will reach these values by that date.")}


@router.get("/{ticker}/forecast-history", summary="All immutable forecasts for a ticker")
def forecast_history(ticker: str, horizon: int | None = Query(None), db: Session = Depends(get_db),
                     mode: str = Depends(current_mode)) -> dict[str, Any]:
    c = require_company(db, ticker, mode)
    q = select(Forecast).where(Forecast.company_id == c.id)
    if horizon:
        q = q.where(Forecast.horizon_months == horizon)
    fs = db.scalars(q.order_by(Forecast.created_at.desc()).limit(300)).all()
    return {"data_mode": mode, "ticker": c.ticker, "forecasts": [forecast_dict(f, include_detail=False) for f in fs]}
