"""Ticker analysis pipeline:

validate → resolve identity → prices (+splits) → SEC financials → news/filings/catalysts →
AI thesis (or rule-based) → quantitative forecasts (all horizons) → persist immutable records.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import DataMode, get_settings
from app.models import (
    Analysis,
    AnalysisSource,
    Company,
    Event,
    EventCompanyRelationship,
    Forecast,
    ForecastScenario,
    ProviderSyncHistory,
    SourceReference,
    StockPrice,
)
from app.providers.market.base import MarketDataProvider
from app.services import ai as ai_mod
from app.services.events import ingest_articles, upcoming_for_company, upsert_upcoming
from app.services.financials import FinancialSnapshot, build_snapshot, load_facts, upsert_facts
from app.services.forecasting.engine import (
    HORIZONS_MONTHS,
    MODEL_VERSION,
    AIAdjustment,
    ForecastEngine,
    ForecastInputs,
    HorizonForecast,
    run_all_horizons,
)
from app.services.forecasting.sectors import profile, sector_from_sic
from app.services.http import DataUnavailable, ProviderError, ProviderNotConfigured
from app.services.market_calendar import last_completed_session, trading_days_stale
from app.services.prices import (
    beta_vs,
    load_adjusted_prices,
    price_stats,
    upsert_corporate_actions,
    upsert_prices,
)
from app.services.providers import FinancialDataSource, edgar_client, market_provider, news_provider

log = logging.getLogger(__name__)

TICKER_RE = re.compile(r"^[A-Z]{1,5}([.-][A-Z]{1,2})?$")
BENCHMARK = "SPY"
HISTORY_DAYS = 730
SUPPORTED_TYPES = {None, "CS", "ADRC", "ADRP", "ADRS", "OS"}


class AnalysisError(Exception):
    def __init__(self, message: str, code: str = "analysis_failed"):
        super().__init__(message)
        self.code = code


def normalize_ticker(raw: str) -> str:
    t = (raw or "").strip().upper()
    if not TICKER_RE.match(t):
        raise AnalysisError(f"'{raw}' is not a valid US ticker symbol format.", "invalid_ticker")
    return t


def _record_sync(session: Session, provider: str, op: str, target: str | None, status: str,
                 records: int | None = None, error: str | None = None, started: datetime | None = None) -> None:
    session.add(ProviderSyncHistory(provider=provider, operation=op, target=target, status=status, records=records,
                                    error=error, started_at=started or datetime.now(UTC),
                                    finished_at=datetime.now(UTC)))


def get_company(session: Session, ticker: str, mode: str) -> Company | None:
    return session.scalar(select(Company).where(Company.ticker == ticker, Company.data_mode == mode))


def get_or_create_company(session: Session, ticker: str, mode: str) -> Company:
    c = get_company(session, ticker, mode)
    if c is None:
        session.execute(insert(Company).values(ticker=ticker, data_mode=mode).on_conflict_do_nothing(
            constraint="uq_company_ticker_mode"))
        session.flush()
        c = get_company(session, ticker, mode)
    assert c is not None
    return c


# ---------------------------------------------------------------------------------
# data refresh steps
# ---------------------------------------------------------------------------------
def refresh_identity(session: Session, company: Company, mode: DataMode, provider: MarketDataProvider | None,
                     warnings: list[str]) -> None:
    fresh = company.profile_updated_at and company.profile_updated_at > datetime.now(UTC) - timedelta(days=1)
    if fresh and company.name:
        return
    t = company.ticker
    if mode == "live":
        try:
            ident = edgar_client().resolve_ticker(t)
            company.cik, company.name = ident.cik, ident.name
            company.exchange = ident.exchange or company.exchange
            subs = edgar_client().submissions(ident.cik)
            company.sic_code = subs.sic
            company.industry = subs.sic_description
            company.fiscal_year_end = subs.fiscal_year_end
            company.name = subs.name or company.name
        except ProviderError as exc:
            warnings.append(f"SEC identity lookup failed: {exc.message}")
    if provider is not None:
        try:
            p = provider.get_company_profile(t)
            if p.security_type not in SUPPORTED_TYPES:
                raise AnalysisError(f"{t} is a {p.security_type} security; only common stocks/ADRs are supported.",
                                    "unsupported_security")
            if not p.active:
                warnings.append(f"{t} is marked inactive/delisted by the market-data provider.")
            company.name = company.name or p.name
            company.exchange = p.exchange or company.exchange
            company.market_cap = p.market_cap
            company.shares_outstanding = p.shares_outstanding
            company.currency = p.currency
            company.cik = company.cik or (p.cik.zfill(10) if p.cik else None)
            company.sic_code = company.sic_code or p.sic_code
            company.industry = company.industry or p.sic_description
            company.profile_source = p.source
        except DataUnavailable as exc:
            if mode == "demo" or not company.cik:
                raise AnalysisError(f"Unknown ticker {t}: {exc.message}", "unknown_ticker") from exc
            warnings.append(f"Market-data provider has no profile for {t}.")
        except ProviderError as exc:
            warnings.append(f"Profile lookup failed: {exc.message}")
    if not company.name:
        raise AnalysisError(f"Could not resolve {t} to a company (not found in SEC or market-data provider).",
                            "unknown_ticker")
    company.sector = sector_from_sic(company.sic_code, company.industry)
    company.profile_updated_at = datetime.now(UTC)


def refresh_prices(session: Session, company: Company, provider: MarketDataProvider | None,
                   warnings: list[str], days: int = HISTORY_DAYS) -> None:
    last = session.scalar(select(StockPrice.trade_date).where(StockPrice.company_id == company.id)
                          .order_by(StockPrice.trade_date.desc()).limit(1))
    first = session.scalar(select(StockPrice.trade_date).where(StockPrice.company_id == company.id)
                           .order_by(StockPrice.trade_date.asc()).limit(1))
    target = last_completed_session()
    want_start = target - timedelta(days=days)
    if last and last >= target and first and first <= want_start + timedelta(days=10):
        return
    if provider is None:
        warnings.append("Live market data unavailable (MASSIVE_API_KEY not set); using stored prices only.")
        return
    started = datetime.now(UTC)
    try:
        start = want_start if not first or first > want_start + timedelta(days=10) else (last or want_start)
        bars = provider.get_historical_prices(company.ticker, start, target)
        n = upsert_prices(session, company.id, bars, provider.name)
        upsert_corporate_actions(session, company.id, provider.get_corporate_actions(company.ticker), provider.name)
        _record_sync(session, provider.name, "historical_prices", company.ticker, "ok", n, started=started)
    except ProviderError as exc:
        _record_sync(session, provider.name, "historical_prices", company.ticker, "error", error=exc.message, started=started)
        warnings.append(f"Price refresh failed ({exc.message}); showing last stored prices if any.")


def refresh_financials(session: Session, company: Company, mode: DataMode, warnings: list[str]) -> dict:
    started = datetime.now(UTC)
    try:
        facts, meta = FinancialDataSource(mode).facts(company.ticker, company.cik)
        n = upsert_facts(session, company.id, facts, source=meta["source"])
        _record_sync(session, meta["source"], "companyfacts", company.ticker, "ok", n, started=started)
        return meta
    except ProviderError as exc:
        _record_sync(session, "sec_edgar", "companyfacts", company.ticker, "error", error=exc.message, started=started)
        warnings.append(f"SEC financial data unavailable: {exc.message}")
        return {"currency": "USD", "source": "sec_edgar", "url": None}


def refresh_news(session: Session, company: Company, mode: DataMode, warnings: list[str]) -> None:
    prov = news_provider(mode, [company.ticker])
    try:
        arts = prov.fetch_company_news(company.ticker) + prov.fetch_sec_filings(company.ticker)
        ingest_articles(session, mode, arts, {company.ticker: company})
        upsert_upcoming(session, mode, prov.fetch_upcoming_events([company.ticker]), {company.ticker: company})
    except ProviderError as exc:
        warnings.append(f"News refresh failed: {exc.message}")
    for e in getattr(prov, "errors", [])[:5]:
        warnings.append(f"News source issue: {e}")


def ensure_benchmark(session: Session, mode: DataMode, provider: MarketDataProvider | None) -> Company:
    bench = get_or_create_company(session, BENCHMARK, mode)
    if not bench.name:
        bench.name = "SPDR S&P 500 ETF Trust (benchmark)" if mode == "live" else "SPY synthetic benchmark (DEMO)"
        bench.sector = "Benchmark"
    refresh_prices(session, bench, provider, [], days=HISTORY_DAYS + 400)
    return bench


# ---------------------------------------------------------------------------------
# metric helpers
# ---------------------------------------------------------------------------------
def historical_pe_band(facts: list, prices: pd.DataFrame) -> tuple[float, float, float] | None:
    if prices.empty:
        return None
    month_ends = prices["close"].resample("ME").last().dropna().tail(24)
    pes = []
    for ts, px in month_ends.items():
        snap = build_snapshot(facts, as_of=ts.date())
        eps = snap.ttm.get("eps")
        if eps and eps > 0:
            pes.append(px / eps)
    if len(pes) < 6:
        return None
    a = np.array(pes)
    return float(np.percentile(a, 25)), float(np.percentile(a, 50)), float(np.percentile(a, 75))


def risk_rating(vol: float | None, bear_return: float | None, net_margin: float | None,
                net_cash: float | None, net_income: float | None, stale_days: int) -> tuple[str, list[str]]:
    pts, why = 0, []
    if vol is not None:
        if vol > 0.45:
            pts, why = pts + 2, why + [f"high volatility ({vol:.0%})"]
        elif vol > 0.30:
            pts, why = pts + 1, why + [f"elevated volatility ({vol:.0%})"]
    if bear_return is not None:
        if bear_return < -0.40:
            pts, why = pts + 2, why + [f"deep bear-case downside ({bear_return:.0%})"]
        elif bear_return < -0.25:
            pts, why = pts + 1, why + [f"meaningful bear-case downside ({bear_return:.0%})"]
    if net_margin is not None and net_margin < 0:
        pts, why = pts + 2, why + ["unprofitable"]
    if net_cash is not None and net_cash < 0 and net_income and net_income > 0 and -net_cash > 3 * net_income:
        pts, why = pts + 1, why + ["net debt > 3× net income"]
    if stale_days > 2:
        pts, why = pts + 1, why + [f"price data {stale_days} sessions old"]
    rating = "Low" if pts <= 1 else "Medium" if pts <= 3 else "High"
    return rating, why or ["no elevated risk factors detected by the screen"]


def quant_outlook(base: float | None, bear: float | None) -> str:
    if base is None:
        return "neutral"
    if base > 0.10 and (bear is None or bear > -0.30):
        return "bullish"
    if base < -0.05:
        return "bearish"
    return "neutral"


def _src_label(i: int) -> str:
    return f"S{i}"


@dataclass
class AnalysisOutcome:
    analysis_id: int
    cached: bool
    warnings: list[str]


def _upsert_source(session: Session, mode: str, stype: str, url: str, title: str | None, publisher: str | None,
                   published_at: datetime | None, accession: str | None = None) -> SourceReference:
    stmt = insert(SourceReference).values(
        data_mode=mode, source_type=stype, url=url, title=title, publisher=publisher, published_at=published_at,
        accession=accession,
    ).on_conflict_do_update(constraint="uq_source_url", set_={"title": title, "retrieved_at": datetime.now(UTC)})
    session.execute(stmt)
    return session.scalar(select(SourceReference).where(SourceReference.data_mode == mode,
                                                        SourceReference.source_type == stype, SourceReference.url == url))


# ---------------------------------------------------------------------------------
def analyze_ticker(session: Session, raw_ticker: str, mode: DataMode, force: bool = False,
                   progress=lambda msg: None, use_ai: bool = True) -> AnalysisOutcome:
    settings = get_settings()
    ticker = normalize_ticker(raw_ticker)
    warnings: list[str] = []

    try:
        provider: MarketDataProvider | None = market_provider(mode)
    except ProviderNotConfigured as exc:
        provider = None
        warnings.append(exc.message)

    progress("Resolving company identity")
    company = get_or_create_company(session, ticker, mode)
    refresh_identity(session, company, mode, provider, warnings)

    progress("Retrieving prices")
    refresh_prices(session, company, provider, warnings)
    bench = ensure_benchmark(session, mode, provider) if ticker != BENCHMARK else company

    progress("Retrieving SEC financial statements")
    fin_meta = refresh_financials(session, company, mode, warnings)
    facts = load_facts(session, company.id)
    snap: FinancialSnapshot = build_snapshot(facts, as_of=date.today(), currency=fin_meta.get("currency", "USD"))
    warnings.extend(snap.warnings)

    progress("Collecting news, filings and catalysts")
    refresh_news(session, company, mode, warnings)
    session.flush()

    prices = load_adjusted_prices(session, company.id, start=date.today() - timedelta(days=HISTORY_DAYS + 30))
    if prices.empty:
        # Keep the identity, SEC financials and news already fetched, but never forecast
        # from a missing price.
        session.commit()
        raise AnalysisError(
            f"No price data available for {ticker}. " + ("Set MASSIVE_API_KEY for live prices." if provider is None else ""),
            "no_price_data")
    last_px = float(prices["close"].iloc[-1])
    last_date = prices.index[-1].date()
    stale = trading_days_stale(last_date)
    if stale > 0:
        warnings.append(f"Latest available price is from {last_date} ({stale} session(s) old).")

    # ---- input hash for caching ---------------------------------------------------
    recent_events = list(session.scalars(
        select(Event).join(EventCompanyRelationship).where(
            EventCompanyRelationship.company_id == company.id,
            Event.last_published_at >= datetime.now(UTC) - timedelta(days=30))
        .order_by(Event.last_published_at.desc()).limit(15)))
    upcoming = upcoming_for_company(session, company.id)
    input_hash = hashlib.sha256(json.dumps({
        "px": [str(last_date), round(last_px, 4)], "fin": snap.financial_data_version,
        "ev": sorted(f"{e.id}:{e.content_hash}:{e.status}" for e in recent_events),
        "up": sorted(f"{e.id}:{e.event_date}" for e in upcoming),
        "model": MODEL_VERSION, "ai": settings.openai_model if (settings.openai_configured and use_ai) else "rule",
    }, sort_keys=True).encode()).hexdigest()

    if not force:
        prev = session.scalar(select(Analysis).where(
            Analysis.company_id == company.id, Analysis.input_hash == input_hash,
            Analysis.created_at >= datetime.now(UTC) - timedelta(hours=settings.analysis_cache_ttl_hours),
        ).order_by(Analysis.created_at.desc()))
        if prev:
            return AnalysisOutcome(prev.id, True, warnings)

    # ---- metrics --------------------------------------------------------------------
    bench_px = load_adjusted_prices(session, bench.id, start=date.today() - timedelta(days=HISTORY_DAYS + 30))
    stats = price_stats(prices)
    beta = beta_vs(prices["close"], bench_px["close"]) if not bench_px.empty else None
    pe_band = historical_pe_band(facts, prices)
    sector = profile(company.sector)
    t = snap.ttm
    cash, debt = t.get("cash_and_investments"), t.get("total_debt")
    net_cash = (cash or 0) - (debt or 0) if cash is not None or debt is not None else None
    eps = t.get("eps")

    # ---- sources --------------------------------------------------------------------
    sources: list[dict[str, Any]] = []
    refs: list[tuple[str, SourceReference]] = []

    def add_source(stype: str, url: str, title: str, publisher: str | None, published: datetime | None,
                   excerpt: str | None = None, accession: str | None = None, ai_type: str | None = None) -> str:
        label = _src_label(len(sources) + 1)
        ref = _upsert_source(session, mode, stype, url, title, publisher, published, accession)
        refs.append((label, ref))
        sources.append({"id": label, "type": ai_type or stype, "title": title, "publisher": publisher,
                        "published_at": published.isoformat() if published else None, "url": url, "excerpt": excerpt})
        return label

    demo = mode == "demo"
    if demo:
        add_source("demo", "https://demo.eventalpha.invalid/financials/" + ticker.lower(),
                   f"{ticker} synthetic financial statements (DEMO)", "EventAlpha demo", None, ai_type="demo_financials")
        add_source("demo", "https://demo.eventalpha.invalid/prices/" + ticker.lower(),
                   f"{ticker} synthetic daily prices (DEMO)", "EventAlpha demo", None, ai_type="demo_market")
    else:
        if fin_meta.get("url"):
            add_source("sec_xbrl", fin_meta["url"], f"SEC XBRL CompanyFacts — {company.name}", "SEC EDGAR",
                       None, f"Latest filing used: {snap.financial_data_version}", ai_type="sec_xbrl")
        add_source("market_data", f"https://massive.com/quote/{ticker}", f"{ticker} daily prices", "Massive", None,
                   f"Close {last_px:.2f} on {last_date}", ai_type="market_data")
    for e in recent_events:
        add_source("news" if e.event_type != "sec_filing" else "sec_filing", e.primary_url or "",
                                 e.title, e.source_name, e.last_published_at,
                                 (e.summary or "")[:500] + (" [UNCONFIRMED REPORT]" if e.status == "unconfirmed" else ""))
    upcoming_dicts = []
    for e in upcoming[:8]:
        sid = add_source("calendar", e.primary_url or "", e.title, e.source_name, None, e.summary)
        upcoming_dicts.append({"title": e.title, "status": e.status, "event_date": str(e.event_date),
                               "event_type": e.event_type, "source_id": sid, "event_id": e.id})

    # ---- baseline quant (no AI adjustments) to inform rule-based / AI --------------
    def inputs(adjs: list[AIAdjustment]) -> ForecastInputs:
        return ForecastInputs(
            ticker=ticker, price=last_px, price_date=last_date, sector=sector,
            revenue_ttm=t.get("revenue"), revenue_growth_yoy=snap.ratios.get("revenue_growth_yoy"),
            revenue_cagr_3y=snap.ratios.get("revenue_cagr_3y"), net_margin=snap.ratios.get("net_margin"),
            operating_margin=snap.ratios.get("operating_margin"), fcf_margin=snap.ratios.get("fcf_margin"),
            eps_ttm=eps, shares=t.get("diluted_shares") or company.shares_outstanding,
            share_change_rate=snap.ratios.get("share_change_rate"), cash=cash, debt=debt,
            margin_history=snap.margin_history, beta=beta, volatility=stats.get("volatility_1y"),
            historical_pe_band=pe_band, currency_ok=snap.currency == "USD" and not snap.foreign_issuer,
            risk_free_rate=settings.risk_free_rate, equity_risk_premium=settings.equity_risk_premium,
            pre_tax_cost_of_debt=settings.pre_tax_cost_of_debt, tax_rate=settings.tax_rate, ai_adjustments=adjs,
        )

    base_engine = ForecastEngine(inputs([]))
    baseline = {h: base_engine.run(h) for h in HORIZONS_MONTHS}
    b12 = baseline[12].scenarios
    metrics = {
        "price": last_px, "price_date": str(last_date), "revenue_ttm": t.get("revenue"),
        "revenue_growth_yoy": snap.ratios.get("revenue_growth_yoy"), "revenue_cagr_3y": snap.ratios.get("revenue_cagr_3y"),
        "eps_ttm": eps, "eps_growth_yoy": snap.ratios.get("eps_growth_yoy"),
        "operating_margin": snap.ratios.get("operating_margin"), "net_margin": snap.ratios.get("net_margin"),
        "fcf_margin": snap.ratios.get("fcf_margin"), "free_cash_flow_ttm": t.get("free_cash_flow"),
        "cash": cash, "debt": debt, "net_cash": net_cash, "pe_ttm": (last_px / eps) if eps and eps > 0 else None,
        "pe_band": [round(x, 2) for x in base_engine.pe_band],
        "volatility_1y": stats.get("volatility_1y"), "beta": beta,
        "base_return_12m": b12["base"].potential_return, "bear_return_12m": b12["bear"].potential_return,
        "sector": company.sector, "fiscal_currency": snap.currency,
    }

    # ---- AI thesis --------------------------------------------------------------------
    progress("Interpreting news and financials")
    usage: dict[str, Any] = {}
    cost = None
    ai_kind = "rule_based"
    ai_model = None
    prompt_version = None
    thesis: dict[str, Any]
    if demo:
        thesis = ai_mod.rule_based_thesis(metrics, sources, upcoming_dicts, demo=True)
        ai_kind = "demo"
        usage = {"skipped": "demo mode never calls OpenAI"}
    elif not use_ai:
        thesis = ai_mod.rule_based_thesis(metrics, sources, upcoming_dicts, demo=False)
        usage = {"skipped": "AI not requested for this run (screening pass)"}
    else:
        engine = ai_mod.AIEngine(settings, session)
        facts_block = {k: v for k, v in metrics.items() if k not in ("base_return_12m", "bear_return_12m", "pe_band")}
        facts_block["upcoming_events"] = upcoming_dicts
        res, usage = engine.stock_thesis(ticker, company.name or ticker, facts_block, sources, force=force)
        if res is not None:
            thesis, ai_kind, ai_model = res, "openai", usage.get("response_model") or settings.openai_model
            prompt_version = ai_mod.PROMPT_VERSION
            cost = usage.get("cost_usd")
        else:
            thesis = ai_mod.rule_based_thesis(metrics, sources, upcoming_dicts, demo=False)
            if usage.get("skipped") or usage.get("error"):
                warnings.append("AI analysis not used: " + (usage.get("skipped") or usage.get("error", "")))

    # ---- final forecasts (with bounded AI adjustments) ---------------------------------
    progress("Computing forecasts")
    adjs = [AIAdjustment(scenario=a["scenario"], revenue_growth_pp=a["revenue_growth_adjustment_pp"],
                         net_margin_pp=a["net_margin_adjustment_pp"], rationale=a["rationale"],
                         source_labels=a["source_ids"]) for a in thesis.get("assumption_suggestions", [])]
    horizons: list[HorizonForecast] = run_all_horizons(inputs(adjs)) if adjs else list(baseline.values())
    h12 = next(h for h in horizons if h.horizon_months == 12)
    base_r, bear_r = h12.scenarios["base"].potential_return, h12.scenarios["bear"].potential_return
    rating, rating_why = risk_rating(stats.get("volatility_1y"), bear_r, snap.ratios.get("net_margin"), net_cash,
                                     t.get("net_income"), stale)

    snapshot = {
        **metrics, "base_return_12m": base_r, "bear_return_12m": bear_r,
        "bull_return_12m": h12.scenarios["bull"].potential_return,
        "base_target_12m": h12.scenarios["base"].target_price, "bear_target_12m": h12.scenarios["bear"].target_price,
        "bull_target_12m": h12.scenarios["bull"].target_price,
        "outlook": quant_outlook(base_r, bear_r), "ai_outlook": thesis.get("outlook"),
        "risk_rating": rating, "risk_reasons": rating_why, "price_stats": stats, "stale_sessions": stale,
        "bullish_points": [c["text"] for c in thesis.get("bullish_arguments", [])][:6],
        "bearish_points": [c["text"] for c in thesis.get("bearish_arguments", [])][:6],
        "upcoming": upcoming_dicts, "financial_data_version": snap.financial_data_version,
        "ttm_period_end": str(snap.ttm_period_end) if snap.ttm_period_end else None,
        "latest_filing_date": str(snap.latest_filed) if snap.latest_filed else None,
    }

    analysis = Analysis(
        company_id=company.id, data_mode=mode, input_hash=input_hash, observed_price=last_px,
        observed_price_date=last_date, ai_kind=ai_kind, ai_model=ai_model, prompt_version=prompt_version,
        thesis=thesis, snapshot=_jsonable(snapshot), usage=_jsonable(usage), cost_usd=cost,
        financial_data_version=snap.financial_data_version, forecast_model_version=MODEL_VERSION,
        warnings=list(dict.fromkeys(warnings)),
    )
    session.add(analysis)
    session.flush()
    for label, ref in refs:
        session.add(AnalysisSource(analysis_id=analysis.id, source_id=ref.id, label=label))
    news_ids = [r.id for _, r in refs]
    for h in horizons:
        persist_forecast(session, analysis, company, h, news_ids, ai_model)
    session.flush()
    return AnalysisOutcome(analysis.id, False, warnings)


def persist_forecast(session: Session, analysis: Analysis, company: Company, h: HorizonForecast,
                     source_ids: list[int], ai_model: str | None) -> Forecast:
    f = Forecast(
        analysis_id=analysis.id, company_id=company.id, data_mode=analysis.data_mode,
        horizon_months=h.horizon_months, target_date=h.target_date, observed_price=h.observed_price,
        observed_price_date=h.observed_price_date, valuation_methods=_jsonable(h.methods),
        assumptions=_jsonable({"items": [a.__dict__ for a in h.assumptions], "warnings": h.warnings,
                               "volatility_range": h.volatility_range}),
        sensitivity=_jsonable(h.sensitivity), financial_data_version=analysis.financial_data_version,
        news_source_ids=source_ids, ai_model=ai_model, forecast_model_version=h.model_version,
    )
    session.add(f)
    session.flush()
    for name, s in h.scenarios.items():
        session.add(ForecastScenario(
            forecast_id=f.id, scenario=name, revenue=s.revenue, revenue_growth=s.revenue_growth_annual,
            operating_margin=s.operating_margin, net_margin=s.net_margin, eps=s.eps, free_cash_flow=s.free_cash_flow,
            pe_multiple=s.pe_multiple, target_price_pe=s.target_price_pe, target_price_dcf=s.target_price_dcf,
            target_price_other=s.target_price_ev_sales, target_price=s.target_price,
            potential_return=s.potential_return, explanation=s.explanation, details=_jsonable(s.details),
        ))
    return f


def _jsonable(obj: Any) -> Any:
    """JSON-safe copy for JSONB columns (dates -> ISO strings, NaN/inf -> null)."""
    return _clean(json.loads(json.dumps(obj, default=_default)))


def _clean(o: Any) -> Any:
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


def _default(o: Any) -> Any:
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if hasattr(o, "__dict__"):
        return o.__dict__
    return str(o)
