"""Forecast evaluation.

1) Realized evaluation: compares every stored forecast whose target date has passed with
   the realized split-adjusted return and the S&P 500 (SPY) return over the same window.
   All forecasts are included — tickers that later lost price coverage (e.g. delisted) are
   reported as unresolved rather than dropped, to avoid survivorship bias.

2) Point-in-time replay: re-runs the quantitative engine at past dates using only prices up
   to that date and SEC facts *filed* on or before that date (no look-ahead), without AI.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Company, Forecast, ForecastScenario
from app.services.analysis import BENCHMARK, get_company, historical_pe_band
from app.services.financials import build_snapshot, load_facts
from app.services.forecasting.engine import ForecastEngine, ForecastInputs
from app.services.forecasting.sectors import profile
from app.services.prices import annualized_volatility, beta_vs, load_adjusted_prices


def price_on_or_after(df: pd.DataFrame, d: date, max_gap_days: int = 7) -> float | None:
    if df.empty:
        return None
    sub = df[df.index >= pd.Timestamp(d)]
    if sub.empty or (sub.index[0] - pd.Timestamp(d)).days > max_gap_days:
        return None
    return float(sub["close"].iloc[0])


def price_on_or_before(df: pd.DataFrame, d: date) -> float | None:
    sub = df[df.index <= pd.Timestamp(d)]
    return float(sub["close"].iloc[-1]) if not sub.empty else None


def realized_evaluation(session: Session, mode: str, horizon: int | None = None) -> dict[str, Any]:
    today = date.today()
    q = select(Forecast, Company).join(Company, Company.id == Forecast.company_id).where(
        Forecast.data_mode == mode, Forecast.target_date <= today)
    if horizon:
        q = q.where(Forecast.horizon_months == horizon)
    rows = session.execute(q.order_by(Forecast.created_at)).all()
    bench = get_company(session, BENCHMARK, mode)
    bench_df = load_adjusted_prices(session, bench.id) if bench else pd.DataFrame()
    price_cache: dict[int, pd.DataFrame] = {}
    items, unresolved = [], []
    for f, c in rows:
        df = price_cache.setdefault(c.id, load_adjusted_prices(session, c.id))
        p0 = price_on_or_before(df, f.observed_price_date)
        p1 = price_on_or_after(df, f.target_date)
        if p0 is None or p1 is None:
            unresolved.append({"forecast_id": f.id, "ticker": c.ticker, "target_date": str(f.target_date),
                               "reason": "no price at target date (data gap or possible delisting)"})
            continue
        sc = {s.scenario: s for s in session.scalars(select(ForecastScenario).where(ForecastScenario.forecast_id == f.id))}
        realized = p1 / p0 - 1
        b0, b1 = price_on_or_before(bench_df, f.observed_price_date), price_on_or_after(bench_df, f.target_date)
        bench_ret = (b1 / b0 - 1) if b0 and b1 else None
        base = sc.get("base")
        items.append({
            "forecast_id": f.id, "ticker": c.ticker, "created_at": f.created_at.isoformat(),
            "horizon_months": f.horizon_months, "target_date": str(f.target_date),
            "predicted_base_return": base.potential_return if base else None,
            "predicted_bear_return": sc["bear"].potential_return if "bear" in sc else None,
            "predicted_bull_return": sc["bull"].potential_return if "bull" in sc else None,
            "realized_return": realized, "benchmark_return": bench_ret,
            "excess_vs_benchmark": realized - bench_ret if bench_ret is not None else None,
        })
    return {"items": items, "unresolved": unresolved, "summary": summarize(items)}


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {"count": 0}
    with_pred = [i for i in items if i["predicted_base_return"] is not None]
    hits = [i for i in with_pred if (i["predicted_base_return"] > 0) == (i["realized_return"] > 0)]
    within = [i for i in items if i.get("predicted_bear_return") is not None and i.get("predicted_bull_return") is not None
              and i["predicted_bear_return"] <= i["realized_return"] <= i["predicted_bull_return"]]
    errs = [abs(i["predicted_base_return"] - i["realized_return"]) for i in with_pred]
    excess = [i["excess_vs_benchmark"] for i in items if i.get("excess_vs_benchmark") is not None]
    return {
        "count": len(items),
        "direction_hit_rate": len(hits) / len(with_pred) if with_pred else None,
        "within_bear_bull_range": len(within) / len(items),
        "mean_abs_error_base": sum(errs) / len(errs) if errs else None,
        "mean_realized_return": sum(i["realized_return"] for i in items) / len(items),
        "mean_benchmark_return": (sum(i["benchmark_return"] for i in items if i["benchmark_return"] is not None)
                                  / max(1, sum(1 for i in items if i["benchmark_return"] is not None))),
        "mean_excess_vs_benchmark": sum(excess) / len(excess) if excess else None,
        "note": "Small samples are not statistically meaningful. Past accuracy does not imply future accuracy.",
    }


def point_in_time_replay(session: Session, ticker: str, mode: str, horizon_months: int = 6,
                         step_days: int = 63) -> dict[str, Any]:
    s = get_settings()
    company = get_company(session, ticker, mode)
    if company is None:
        return {"items": [], "summary": {"count": 0}, "error": f"{ticker} has not been analyzed yet."}
    prices = load_adjusted_prices(session, company.id)
    bench = get_company(session, BENCHMARK, mode)
    bench_df = load_adjusted_prices(session, bench.id) if bench else pd.DataFrame()
    facts = load_facts(session, company.id)
    if prices.empty or not facts:
        return {"items": [], "summary": {"count": 0}, "error": "insufficient stored data"}
    last = prices.index[-1].date()
    start = prices.index[0].date() + timedelta(days=200)  # need some history for volatility/P/E band
    items = []
    d = start
    while d + relativedelta(months=horizon_months) <= last:
        hist = prices[prices.index <= pd.Timestamp(d)]
        if len(hist) < 120:
            d += timedelta(days=step_days)
            continue
        snap = build_snapshot(facts, as_of=d)  # only facts filed on/before d
        if not snap.has_data:
            d += timedelta(days=step_days)
            continue
        px = float(hist["close"].iloc[-1])
        px_date = hist.index[-1].date()
        bh = bench_df[bench_df.index <= pd.Timestamp(d)] if not bench_df.empty else bench_df
        t = snap.ttm
        inp = ForecastInputs(
            ticker=ticker, price=px, price_date=px_date, sector=profile(company.sector),
            revenue_ttm=t.get("revenue"), revenue_growth_yoy=snap.ratios.get("revenue_growth_yoy"),
            revenue_cagr_3y=snap.ratios.get("revenue_cagr_3y"), net_margin=snap.ratios.get("net_margin"),
            operating_margin=snap.ratios.get("operating_margin"), fcf_margin=snap.ratios.get("fcf_margin"),
            eps_ttm=t.get("eps"), shares=t.get("diluted_shares"), share_change_rate=snap.ratios.get("share_change_rate"),
            cash=t.get("cash_and_investments"), debt=t.get("total_debt"), margin_history=snap.margin_history,
            beta=beta_vs(hist["close"], bh["close"]) if not bh.empty else None,
            volatility=annualized_volatility(hist["close"]),
            historical_pe_band=historical_pe_band([f for f in facts if f.filed_date <= d], hist),
            currency_ok=snap.currency == "USD" and not snap.foreign_issuer, risk_free_rate=s.risk_free_rate,
            equity_risk_premium=s.equity_risk_premium, pre_tax_cost_of_debt=s.pre_tax_cost_of_debt, tax_rate=s.tax_rate,
        )
        hf = ForecastEngine(inp).run(horizon_months)
        p1 = price_on_or_after(prices, hf.target_date)
        b0 = price_on_or_before(bench_df, px_date) if not bench_df.empty else None
        b1 = price_on_or_after(bench_df, hf.target_date) if not bench_df.empty else None
        if p1 is not None:
            items.append({
                "as_of": str(px_date), "target_date": str(hf.target_date), "horizon_months": horizon_months,
                "price": px, "financial_data_as_of": str(snap.latest_filed),
                "predicted_base_return": hf.scenarios["base"].potential_return,
                "predicted_bear_return": hf.scenarios["bear"].potential_return,
                "predicted_bull_return": hf.scenarios["bull"].potential_return,
                "realized_return": p1 / px - 1,
                "benchmark_return": (b1 / b0 - 1) if b0 and b1 else None,
                "excess_vs_benchmark": (p1 / px - 1) - (b1 / b0 - 1) if b0 and b1 else None,
            })
        d += timedelta(days=step_days)
    return {"ticker": ticker, "items": items, "summary": summarize(items),
            "method": "Point-in-time replay: prices ≤ as-of date, SEC facts filed ≤ as-of date, no AI adjustments."}
