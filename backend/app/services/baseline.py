"""Universe-wide baseline data: prices, liquidity and SEC financials for EVERY universe stock,
collected before (and independently of) candidate selection.

Rate-limit friendly: live prices come from Massive's grouped-daily endpoint (one request per
session returns every US ticker), so a 22-session baseline for the whole universe costs ~22
requests the first time and ~1 per day afterwards. SEC financials are refreshed at most once per
`max_financial_age` per company and served from the PostgreSQL response cache otherwise.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import DataMode, get_settings
from app.models import Company, CompanyBaseline, StockPrice
from app.services.discovery_scoring import data_freshness, financial_strength
from app.services.financials import build_snapshot, load_facts, upsert_facts
from app.services.http import ProviderError, ProviderNotConfigured
from app.services.market_calendar import last_completed_session, sessions_between, trading_days_stale
from app.services.prices import load_adjusted_prices, upsert_prices
from app.services.providers import FinancialDataSource, market_provider

log = logging.getLogger(__name__)

BASELINE_SESSIONS = 22  # ~1 trading month: enough for a 20-day average dollar volume
MAX_PRICE_STALENESS = 5  # sessions
BASELINE_WEIGHTS = {"financial_strength": 0.75, "data_freshness": 0.25}


def get_baseline(session: Session, company: Company) -> CompanyBaseline:
    b = session.get(CompanyBaseline, company.id)
    if b is None:
        session.execute(insert(CompanyBaseline).values(company_id=company.id, data_mode=company.data_mode,
                                                       eligible=False).on_conflict_do_nothing())
        session.flush()
        b = session.get(CompanyBaseline, company.id)
    assert b is not None
    return b


def _refresh_prices(session: Session, mode: DataMode, companies: dict[str, Company], progress: Callable[[str], None],
                    stats: dict[str, Any]) -> None:
    try:
        provider = market_provider(mode)
    except ProviderNotConfigured as exc:
        stats["price_error"] = exc.message
        return
    end = last_completed_session()
    sessions = sessions_between(end - timedelta(days=45), end)[-BASELINE_SESSIONS:]
    ids = [c.id for c in companies.values()]
    if provider.is_demo:  # no rate limit: per-ticker history
        for t, c in companies.items():
            upsert_prices(session, c.id, provider.get_historical_prices(t, sessions[0], end), provider.name)
        stats["price_requests"] = 0
        return
    # Grouped daily: skip sessions already stored for most of the universe.
    have = dict(session.execute(
        select(StockPrice.trade_date, func.count()).where(StockPrice.company_id.in_(ids),
                                                          StockPrice.trade_date.in_(sessions))
        .group_by(StockPrice.trade_date)).all())
    missing = [d for d in sessions if have.get(d, 0) < 0.8 * len(ids)]
    stats["price_requests"] = 0
    for n, d in enumerate(missing, 1):
        progress(f"Baseline prices {d} ({n}/{len(missing)})")
        try:
            grouped = provider.get_grouped_daily(d)
        except ProviderError as exc:
            stats.setdefault("price_errors", []).append(f"{d}: {exc.message}")
            continue
        stats["price_requests"] += 1
        for t, c in companies.items():
            bar = grouped.get(t) or grouped.get(t.replace("-", "."))
            if bar:
                upsert_prices(session, c.id, [bar], provider.name)
        session.commit()


def _refresh_financials(session: Session, mode: DataMode, companies: dict[str, Company],
                        progress: Callable[[str], None], max_age: timedelta, stats: dict[str, Any]) -> None:
    now = datetime.now(UTC)
    src = FinancialDataSource(mode)
    todo = [(t, c) for t, c in companies.items()
            if (b := get_baseline(session, c)).financials_fetched_at is None or now - b.financials_fetched_at > max_age]
    stats["financials_refreshed"] = 0
    for n, (t, c) in enumerate(todo, 1):
        if n % 10 == 1:
            progress(f"Baseline SEC financials ({n}/{len(todo)})")
        b = get_baseline(session, c)
        try:
            facts, _meta = src.facts(t, c.cik)
            upsert_facts(session, c.id, facts, source=_meta["source"])
            b.financials_fetched_at = now
            stats["financials_refreshed"] += 1
        except ProviderError as exc:
            b.errors = {**(b.errors or {}), "financials": exc.message}
            b.financials_fetched_at = now  # don't hammer a failing source; retried after max_age
        session.commit()


def compute_baseline(session: Session, company: Company, b: CompanyBaseline, min_adv: float) -> None:
    """Derive baseline metrics, eligibility and score from stored prices + SEC facts."""
    prices = load_adjusted_prices(session, company.id, start=date.today() - timedelta(days=60))
    reasons: list[str] = []
    stale = None
    if prices.empty:
        b.price = b.price_date = b.avg_dollar_volume_20d = None
        b.price_sessions = 0
        reasons.append("no price data")
    else:
        b.price = float(prices["close"].iloc[-1])
        b.price_date = prices.index[-1].date()
        b.price_sessions = int(len(prices))
        tail = prices.tail(20)
        b.avg_dollar_volume_20d = float((tail["close"] * tail["volume"]).mean()) if tail["volume"].notna().any() else None
        stale = trading_days_stale(b.price_date)
        if stale > MAX_PRICE_STALENESS:
            reasons.append(f"price is {stale} sessions old")
        if b.avg_dollar_volume_20d is None or b.avg_dollar_volume_20d < min_adv:
            reasons.append(f"illiquid (20d avg dollar volume {b.avg_dollar_volume_20d or 0:,.0f} < {min_adv:,.0f})")

    snap = build_snapshot(load_facts(session, company.id), as_of=date.today())
    t = snap.ttm
    cash, debt = t.get("cash_and_investments"), t.get("total_debt")
    metrics = {
        "revenue_ttm": t.get("revenue"), "revenue_growth_yoy": snap.ratios.get("revenue_growth_yoy"),
        "net_margin": snap.ratios.get("net_margin"), "fcf_margin": snap.ratios.get("fcf_margin"),
        "net_cash": ((cash or 0) - (debt or 0)) if cash is not None or debt is not None else None,
        "currency": snap.currency, "foreign_issuer": snap.foreign_issuer, "stale_sessions": stale,
    }
    if not snap.has_data:
        reasons.append("no SEC financial statements")
    elif snap.currency != "USD" or snap.foreign_issuer:
        reasons.append("per-share valuation unavailable (non-USD or foreign-issuer ADR)")
    b.financials_as_of = snap.latest_filed
    filing_age = (date.today() - snap.latest_filed).days if snap.latest_filed else None
    fs = financial_strength(metrics)
    fresh = data_freshness(stale, filing_age)
    metrics.update({"financial_strength": fs, "data_freshness": fresh, "filing_age_days": filing_age})
    parts = {k: v for k, v in (("financial_strength", fs), ("data_freshness", fresh)) if v is not None}
    total_w = sum(BASELINE_WEIGHTS[k] for k in parts)
    metrics["baseline_score"] = sum(BASELINE_WEIGHTS[k] * v for k, v in parts.items()) / total_w if total_w else None
    b.metrics = metrics
    b.eligible = not reasons
    b.ineligible_reason = "; ".join(reasons) or None


def refresh_baseline(session: Session, mode: DataMode, companies: dict[str, Company],
                     progress: Callable[[str], None] = lambda m: None,
                     max_financial_age: timedelta = timedelta(hours=24)) -> dict[str, Any]:
    s = get_settings()
    stats: dict[str, Any] = {"universe": len(companies)}
    progress("Baseline prices")
    _refresh_prices(session, mode, companies, progress, stats)
    _refresh_financials(session, mode, companies, progress, max_financial_age, stats)
    progress("Computing baselines")
    for c in companies.values():
        compute_baseline(session, c, get_baseline(session, c), s.min_avg_dollar_volume)
    session.commit()
    rows = [get_baseline(session, c) for c in companies.values()]
    stats["eligible"] = sum(1 for b in rows if b.eligible)
    stats["with_prices"] = sum(1 for b in rows if b.price is not None)
    stats["with_financials"] = sum(1 for b in rows if b.financials_as_of is not None)
    return stats
