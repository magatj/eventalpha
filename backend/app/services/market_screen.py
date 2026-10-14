"""Stage A — low-cost, market-wide screening of the master universe.

Data (all bulk, incremental and rate-limit friendly):
  * prices: Massive grouped daily (1 request returns every US ticker for a session), stored in
    security_daily_bars; only missing sessions are fetched
  * fundamentals: SEC XBRL *frames* (1 request returns one metric for every filer), stored per
    CIK in security_fundamentals; refreshed at most daily
  * sector: SEC submissions (SIC), looked up only for securities that pass size/liquidity

Nothing is fabricated: a metric that can't be computed from these inputs is None, and
eligibility/completeness record exactly what was missing.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import DataMode, get_settings
from app.models import Security, SecurityDailyBar, SecurityFundamentals
from app.services.discovery_scoring import data_freshness, financial_strength
from app.services.forecasting.valuation_registry import business_model
from app.services.http import ProviderError, ProviderNotConfigured
from app.services.market_calendar import last_completed_session, sessions_between, trading_days_stale
from app.services.master_universe import ADR_TYPES, PIPELINE_TYPES, classify_from_sic
from app.services.providers import market_provider
from app.services.screen_coverage import merged_fundamentals, resolve_market_cap, splits_by_ticker

log = logging.getLogger(__name__)

SCREEN_TYPES = PIPELINE_TYPES | ADR_TYPES
SPLIT_JUMP = math.log(1.8)  # a one-day move this large is treated as a possible unadjusted split
# Annual (duration) frames and the instant frames used for screening. Each metric has several
# concepts; the first one a filer reports wins (the concept is kept with the value).
REVENUE_CONCEPTS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                    "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense",
                    "RegulatedAndUnregulatedOperatingRevenue", "RealEstateRevenueNet"]
NET_INCOME_CONCEPTS = ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"]
# industry revenue components, combined only when a filer reports no total-revenue concept
REVENUE_COMPONENTS = {"bank": ("InterestIncomeExpenseNet", "NoninterestIncome",
                               "bank revenue = net interest income + noninterest income"),
                      "insurance": ("PremiumsEarnedNet", "NetInvestmentIncome",
                                    "insurance revenue = net premiums earned + net investment income")}
ANNUAL_FRAMES = {"operating_income": ["OperatingIncomeLoss"],
                 "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
                 "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
                 "capex_productive": ["PaymentsToAcquireProductiveAssets"],
                 "capex_flight": ["PaymentsForFlightEquipment"],
                 "capex_oil_gas": ["PaymentsToAcquireOilAndGasPropertyAndEquipment"]}
INSTANT_FRAMES = {"cash": ("us-gaap", ["CashAndCashEquivalentsAtCarryingValue"], "USD"),
                  "long_term_debt": ("us-gaap", ["LongTermDebtNoncurrent", "LongTermDebt"], "USD"),
                  "equity": ("us-gaap", ["StockholdersEquity",
                                         "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"], "USD"),
                  "shares_outstanding_bs": ("us-gaap", ["CommonStockSharesOutstanding"], "shares")}
SHARE_FRAME = ("shares_outstanding", "dei", ["EntityCommonStockSharesOutstanding"], "shares")
SHARE_FRAME_QUARTERS = 4  # cover-page counts: look back up to four quarters (older ones flagged stale)
COMPLETENESS_KEYS = ("price", "avg_dollar_volume_20d", "market_cap", "revenue", "revenue_prior", "net_income",
                     "free_cash_flow", "cash", "shares_outstanding", "volatility")
NO_FCF_SECTORS = {"Financials", "Real Estate"}  # debt/working capital is operating capital


# ------------------------------------------------------------------------------------- data
def screen_securities(session: Session, mode: DataMode) -> list[Security]:
    return list(session.scalars(select(Security).where(
        Security.data_mode == mode, Security.active, Security.security_type.in_(SCREEN_TYPES),
        Security.eligibility_status != "excluded_type")))


def refresh_market_bars(session: Session, mode: DataMode, progress: Callable[[str], None] = lambda m: None
                        ) -> dict[str, Any]:
    """Store grouped-daily bars for the last N sessions, fetching only sessions not yet stored."""
    s = get_settings()
    stats: dict[str, Any] = {"requests": 0, "sessions_fetched": [], "errors": []}
    try:
        provider = market_provider(mode)
    except ProviderNotConfigured as exc:
        stats["errors"].append(exc.message)
        return stats
    secs = {x.ticker: x.id for x in screen_securities(session, mode)}
    if not secs:
        return stats
    end = last_completed_session()
    sessions = sessions_between(end - timedelta(days=int(s.screen_history_sessions * 1.6) + 10), end)
    sessions = sessions[-s.screen_history_sessions:]
    have = dict(session.execute(select(SecurityDailyBar.trade_date, func.count())
                                .where(SecurityDailyBar.trade_date.in_(sessions)).group_by(SecurityDailyBar.trade_date)).all())
    missing = [d for d in sessions if have.get(d, 0) < 0.5 * len(secs)]
    for n, d in enumerate(missing, 1):
        progress(f"Market prices {d} ({n}/{len(missing)})")
        try:
            grouped = provider.get_grouped_daily(d, adjusted=True)
        except ProviderError as exc:
            stats["errors"].append(f"{d}: {exc.message}")
            continue
        stats["requests"] += 1
        rows = [dict(security_id=secs[t], trade_date=d, close=b.close, volume=b.volume, vwap=b.vwap)
                for t, b in grouped.items() if t in secs]
        for i in range(0, len(rows), 2000):
            stmt = insert(SecurityDailyBar).values(rows[i:i + 2000])
            session.execute(stmt.on_conflict_do_update(
                index_elements=["security_id", "trade_date"],
                set_={"close": stmt.excluded.close, "volume": stmt.excluded.volume, "vwap": stmt.excluded.vwap}))
        session.commit()
        stats["sessions_fetched"].append(str(d))
    stats["sessions_stored"] = len(sessions) - len(missing) + len(stats["sessions_fetched"])
    return stats


def _share_periods(today: date, n: int = SHARE_FRAME_QUARTERS) -> list[str]:
    out, y, q = [], today.year, (today.month - 1) // 3 + 1
    while len(out) < n:
        q -= 1
        if q == 0:
            y, q = y - 1, 4
        qend = date(y, q * 3, 30 if q in (2, 3) else 31)
        if (today - qend).days >= 20:
            out.append(f"CY{y}Q{q}I")
    return out


def _frame_periods(today: date) -> tuple[str, str, list[str]]:
    """Latest complete calendar year (filed by ~April), the prior year, and candidate instant
    quarters (most recent first, ending >= 50 days ago so most filers have reported)."""
    annual = today.year - 1 if today >= date(today.year, 4, 15) else today.year - 2
    quarters = []
    y, q = today.year, (today.month - 1) // 3 + 1
    for _ in range(6):
        q -= 1
        if q == 0:
            y, q = y - 1, 4
        qend = date(y, q * 3, 30 if q in (2, 3) else 31)
        if (today - qend).days >= 50:
            quarters.append(f"CY{y}Q{q}I")
    return f"CY{annual}", f"CY{annual - 1}", quarters[:2]


def refresh_fundamentals(session: Session, mode: DataMode, progress: Callable[[str], None] = lambda m: None,
                         max_age: timedelta = timedelta(hours=20)) -> dict[str, Any]:
    stats: dict[str, Any] = {"requests": 0, "filers": 0, "errors": []}
    latest = session.scalar(select(func.max(SecurityFundamentals.fetched_at)).where(SecurityFundamentals.data_mode == mode))
    if latest and datetime.now(UTC) - latest < max_age:
        stats["skipped"] = f"fresh (fetched {latest:%Y-%m-%d %H:%M} UTC)"
        return stats
    if mode == "demo":
        per_cik = _demo_fundamentals(session)
    else:
        per_cik = _sec_frame_fundamentals(progress, stats)
    rows = [dict(cik=cik, data_mode=mode, metrics=m["metrics"], periods=m["periods"], fetched_at=datetime.now(UTC))
            for cik, m in per_cik.items()]
    for i in range(0, len(rows), 1000):
        stmt = insert(SecurityFundamentals).values(rows[i:i + 1000])
        session.execute(stmt.on_conflict_do_update(index_elements=["cik", "data_mode"], set_={
            "metrics": stmt.excluded.metrics, "periods": stmt.excluded.periods, "fetched_at": stmt.excluded.fetched_at}))
    session.commit()
    stats["filers"] = len(rows)
    return stats


def _sec_frame_fundamentals(progress: Callable[[str], None], stats: dict[str, Any]) -> dict[str, dict]:
    from app.providers.sec.edgar import frame
    from app.services.providers import edgar_client

    client = edgar_client()
    annual, prior, quarters = _frame_periods(date.today())
    out: dict[str, dict] = defaultdict(lambda: {"metrics": {}, "periods": {"annual": annual, "prior": prior}})

    def take(metric: str, tax: str, concepts: list[str], unit: str, period: str) -> int:
        n = 0
        for concept in concepts:
            progress(f"SEC frames {concept} {period}")
            try:
                data = frame(client, tax, concept, unit, period)
            except ProviderError as exc:
                stats["errors"].append(f"{concept}/{period}: {exc.message}")
                continue
            stats["requests"] += 1
            for r in data:
                cik = str(r["cik"]).zfill(10)
                m = out[cik]["metrics"]
                if metric not in m:  # first (highest-priority) concept wins
                    m[metric] = {"val": r["val"], "end": r.get("end"), "start": r.get("start"),
                                 "accn": r.get("accn"), "concept": concept, "period": period}
                    n += 1
        return n

    take("revenue", "us-gaap", REVENUE_CONCEPTS, "USD", annual)
    take("revenue_prior", "us-gaap", REVENUE_CONCEPTS, "USD", prior)
    take("net_income", "us-gaap", NET_INCOME_CONCEPTS, "USD", annual)
    take("net_income_prior", "us-gaap", NET_INCOME_CONCEPTS, "USD", prior)
    for metric, concepts in ANNUAL_FRAMES.items():
        take(metric, "us-gaap", concepts, "USD", annual)
    for kind, (a, b, _) in REVENUE_COMPONENTS.items():
        take(f"_{kind}_a", "us-gaap", [a], "USD", annual)
        take(f"_{kind}_b", "us-gaap", [b], "USD", annual)
    for metric, (tax, concepts, unit) in INSTANT_FRAMES.items():
        for q in quarters:  # most recent quarter first; older quarter fills filers not yet reported
            take(metric, tax, concepts, unit, q)
    share_q = _share_periods(date.today())
    for q in share_q:
        take(SHARE_FRAME[0], SHARE_FRAME[1], SHARE_FRAME[2], SHARE_FRAME[3], q)
    for m in out.values():
        m["periods"]["instant"] = quarters
        m["periods"]["shares"] = share_q
        combine_revenue_components(m["metrics"])
    return dict(out)


def combine_revenue_components(m: dict[str, Any]) -> None:
    """Bank / insurer revenue from components when no total-revenue concept was reported (in place)."""
    for kind, (_, _, label) in REVENUE_COMPONENTS.items():
        a, b = m.pop(f"_{kind}_a", None), m.pop(f"_{kind}_b", None)
        if "revenue" not in m and a and b and a.get("end") == b.get("end"):
            m["revenue"] = {"val": a["val"] + b["val"], "end": a.get("end"), "start": a.get("start"),
                            "accn": a.get("accn"), "concept": label, "period": a.get("period")}


def _demo_fundamentals(session: Session) -> dict[str, dict]:
    from app.demo.synthetic import DemoMarketProvider, demo_financial_facts
    from app.services.financials import build_snapshot

    as_of = DemoMarketProvider().as_of
    out = {}
    for sec in session.scalars(select(Security).where(Security.data_mode == "demo", Security.cik.is_not(None))):
        snap = build_snapshot(demo_financial_facts(sec.ticker, as_of), as_of=as_of)
        ann = snap.annual.get("revenue", [])
        ni = snap.annual.get("net_income", [])
        t = snap.ttm

        def v(x: float | None) -> dict | None:
            return {"val": x, "concept": "demo", "period": "demo"} if x is not None else None

        metrics = {
            "revenue": v(ann[-1].value if ann else None), "revenue_prior": v(ann[-2].value if len(ann) > 1 else None),
            "net_income": v(ni[-1].value if ni else None), "net_income_prior": v(ni[-2].value if len(ni) > 1 else None),
            "operating_income": v(t.get("operating_income")), "operating_cash_flow": v(t.get("operating_cash_flow")),
            "capex": v(t.get("capex")), "cash": v(t.get("cash_and_investments")), "long_term_debt": v(t.get("total_debt")),
            "shares_outstanding": v(t.get("diluted_shares")),
            "equity": v(snap.instant["stockholders_equity"].value if "stockholders_equity" in snap.instant else None),
        }
        out[sec.cik] = {"metrics": {k: x for k, x in metrics.items() if x}, "periods": {"demo": True}}
    return out


# ------------------------------------------------------------------------------------- screening
@dataclass
class ScreenResult:
    security_id: int
    ticker: str
    status: str  # eligible | catalyst_eligible | ineligible | needs_alt_fundamentals
    reason: str | None
    metrics: dict[str, Any] = field(default_factory=dict)
    market_cap: float | None = None
    coverage: str = "none"
    codes: list[str] = field(default_factory=list)  # every failing eligibility check
    market_cap_info: dict[str, Any] | None = None
    financial_score: float | None = None
    valuation_score: float | None = None
    completeness: float = 0.0
    freshness: float | None = None


def price_metrics(closes: list[float], volumes: list[float | None], dates: list[date]) -> dict[str, Any]:
    out: dict[str, Any] = {"sessions": len(closes)}
    if not closes:
        return out
    out["price"], out["price_date"] = closes[-1], dates[-1].isoformat()
    dv = [c * v for c, v in zip(closes[-20:], volumes[-20:], strict=False) if v is not None]
    out["avg_dollar_volume_20d"] = sum(dv) / len(dv) if len(dv) >= min(10, len(closes)) else None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:], strict=False) if a > 0 and b > 0]
    if any(abs(r) > SPLIT_JUMP for r in rets):
        out["data_flags"] = ["possible unadjusted split or data error in price history"]
        return out  # momentum/volatility would be meaningless
    if len(closes) >= 22:
        out["momentum_1m"] = closes[-1] / closes[-22] - 1
    if len(closes) >= 63:
        out["momentum_3m"] = closes[-1] / closes[-63] - 1
    if len(rets) >= 19:
        mean = sum(rets) / len(rets)
        out["volatility"] = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252)
    return out


_AUTO = object()


def fundamental_metrics(f: dict[str, Any] | None, price: float | None, market_cap: Any = _AUTO) -> dict[str, Any]:
    """Screening ratios from SEC fundamentals. `market_cap` comes from resolve_market_cap(); when not
    given it is price x the reported shares outstanding."""
    if not f:
        return {}
    v = {k: (x or {}).get("val") for k, x in f.items() if not k.startswith("_")}
    out: dict[str, Any] = {k: v.get(k) for k in ("revenue", "revenue_prior", "net_income", "net_income_prior",
                                                 "operating_income", "cash", "shares_outstanding", "equity")}
    debt = v.get("long_term_debt")
    out["debt"] = debt
    rev, rev0, ni, ni0 = v.get("revenue"), v.get("revenue_prior"), v.get("net_income"), v.get("net_income_prior")
    ocf = v.get("operating_cash_flow")
    comps = [abs(x) for x in (v.get("capex"), v.get("capex_flight"), v.get("capex_oil_gas")) if x is not None]
    capex_parts = [x for x in (abs(v["capex_productive"]) if v.get("capex_productive") is not None else None,
                               sum(comps) if comps else None) if x is not None]
    capex = max(capex_parts) if capex_parts else None
    out["free_cash_flow"] = ocf - abs(capex) if ocf is not None and capex is not None else None
    out["revenue_growth_yoy"] = rev / rev0 - 1 if rev and rev0 and rev0 > 0 else None
    out["earnings_growth_yoy"] = ni / ni0 - 1 if ni is not None and ni0 and ni0 > 0 and ni > 0 else None
    out["net_margin"] = ni / rev if ni is not None and rev and rev > 0 else None
    out["fcf_margin"] = out["free_cash_flow"] / rev if out["free_cash_flow"] is not None and rev and rev > 0 else None
    out["net_cash"] = (v.get("cash") or 0) - (debt or 0) if v.get("cash") is not None or debt is not None else None
    out["revenue_ttm"] = rev
    eq = v.get("equity")
    out["debt_to_equity"] = debt / eq if debt is not None and eq and eq > 0 else None
    shares = v.get("shares_outstanding")
    if market_cap is _AUTO:
        mcap = price * shares if price and shares and shares > 0 else None
    else:
        mcap = market_cap
    out["market_cap"] = mcap
    if mcap:
        out["earnings_yield"] = ni / mcap if ni is not None else None
        out["fcf_yield"] = out["free_cash_flow"] / mcap if out["free_cash_flow"] is not None else None
        oi = v.get("operating_income")
        ev = mcap + (debt or 0) - (v.get("cash") or 0)
        out["ebit_to_ev"] = oi / ev if oi is not None and ev > 0 else None
        out["book_to_market"] = eq / mcap if eq and eq > 0 else None
    out["earnings_consistency"] = (
        None if ni is None or ni0 is None else 1.0 if ni > 0 and ni0 > 0 else 0.5 if ni > 0 or ni0 > 0 else 0.0)
    return out


def financial_leader_score(m: dict[str, Any]) -> float | None:
    """Profitability, growth, FCF and balance sheet (six-factor definition) + earnings consistency."""
    fs = financial_strength(m)
    if fs is None:
        return None
    cons = m.get("earnings_consistency")
    return fs if cons is None else 0.8 * fs + 0.2 * cons


def _percentiles(values: dict[int, float]) -> dict[int, float]:
    if not values:
        return {}
    ordered = sorted(values, key=lambda k: values[k])
    n = len(ordered)
    return {k: (i + 0.5) / n for i, k in enumerate(ordered)}


def sector_relative_valuation(results: list[ScreenResult], sectors: dict[int, str | None],
                              min_group: int = 8) -> None:
    """Valuation score = mean within-sector percentile of available yields (higher yield = cheaper).

    Sector-aware: banks/insurers/REITs skip FCF and EBIT/EV (debt is operating capital); REITs
    get no score (they need FFO-based valuation). Small sectors fall back to the whole market."""
    pool = [r for r in results if r.status in ("eligible", "catalyst_eligible")]
    by_sector: dict[str, list[ScreenResult]] = defaultdict(list)
    for r in pool:
        by_sector[sectors.get(r.security_id) or "Unknown"].append(r)
    metrics = ("earnings_yield", "fcf_yield", "ebit_to_ev")
    market_pct = {m: _percentiles({r.security_id: r.metrics[m] for r in pool
                                   if r.metrics.get(m) is not None and (r.metrics.get("net_income") or 0) > 0})
                  for m in metrics + ("book_to_market",)}
    for sector, group in by_sector.items():
        if sector == "Real Estate":
            for r in group:
                r.metrics["valuation_note"] = ("REIT: valued on P/FFO in deep analysis; no screening score "
                                               "(FFO needs depreciation, not in the bulk screen)")
            continue
        # banks/insurers: earnings yield and book-to-market (FCF and EV are not meaningful)
        use = ("earnings_yield", "book_to_market") if sector in NO_FCF_SECTORS else metrics
        sector_pct = {m: _percentiles({r.security_id: r.metrics[m] for r in group
                                       if r.metrics.get(m) is not None and (r.metrics.get("net_income") or 0) > 0})
                      for m in use}
        for r in group:
            vals = []
            for m in use:
                src = sector_pct[m] if len(sector_pct[m]) >= min_group else market_pct[m]
                if r.security_id in src:
                    vals.append(src[r.security_id])
            r.valuation_score = sum(vals) / len(vals) if vals else None
            r.metrics["valuation_basis"] = (f"{sector} peers" if all(len(sector_pct[m]) >= min_group for m in use)
                                           else "market-wide (small sector)")


def screen(session: Session, mode: DataMode, catalysts: dict[int, float] | None = None,
           progress: Callable[[str], None] = lambda m: None, enrich_sic: bool = True) -> list[ScreenResult]:
    """Apply eligibility thresholds and compute screening scores for every active security."""
    s = get_settings()
    catalysts = catalysts or {}
    secs = screen_securities(session, mode)
    by_id = {x.id: x for x in secs}
    since = date.today() - timedelta(days=int(s.screen_history_sessions * 1.6) + 15)
    bars: dict[int, tuple[list, list, list]] = defaultdict(lambda: ([], [], []))
    for sid, d, c, v in session.execute(
            select(SecurityDailyBar.security_id, SecurityDailyBar.trade_date, SecurityDailyBar.close,
                   SecurityDailyBar.volume).where(SecurityDailyBar.trade_date >= since)
            .order_by(SecurityDailyBar.security_id, SecurityDailyBar.trade_date)):
        if sid in by_id:
            b = bars[sid]
            b[0].append(c), b[1].append(v), b[2].append(d)
    funds = {f.cik: (f.metrics, f.fallback) for f in session.scalars(select(SecurityFundamentals).where(
        SecurityFundamentals.data_mode == mode))}
    split_map = splits_by_ticker(session, mode)
    class_prices: dict[str, list[float]] = defaultdict(list)  # listed share classes per filer
    for x in secs:
        b = bars.get(x.id)
        if x.cik and x.security_type in PIPELINE_TYPES and b and b[0]:
            class_prices[x.cik].append(b[0][-1])
    stale_cache: dict[date, int] = {}
    results: list[ScreenResult] = []
    for sec in secs:
        closes, vols, dates = bars.get(sec.id, ([], [], []))
        pm = price_metrics(closes, vols, dates)
        frames, fallback = funds.get(sec.cik, (None, None)) if sec.cik else (None, None)
        merged = merged_fundamentals(frames, fallback)
        mc = resolve_market_cap(pm.get("price"), merged or {}, fallback, sec.provider_shares, sec.provider_checked_at,
                                split_map.get(sec.ticker), class_prices.get(sec.cik) if sec.cik else None)
        fm = fundamental_metrics(merged, pm.get("price"), market_cap=mc.value) if merged else {}
        if merged:
            fm["market_cap_info"] = mc.to_dict()
            fm["fundamentals_source"] = ("frames+companyfacts" if frames and fallback and any(
                k in fallback and k not in frames for k in ("revenue", "net_income", "shares_outstanding"))
                else "companyfacts" if not frames else "frames")
            if (fallback or {}).get("_validation"):
                fm["validation"] = fallback["_validation"]  # informational; only hard errors exclude
            if (fallback or {}).get("_status"):
                fm["financial_status"] = fallback["_status"]
        m = {**pm, **fm}
        if pm.get("price_date"):
            d0 = date.fromisoformat(pm["price_date"])
            stale_cache.setdefault(d0, trading_days_stale(d0))
            m["stale_sessions"] = stale_cache[d0]
        cov = ("foreign" if sec.security_type in ADR_TYPES and not fm else
               "sec_full" if fm.get("revenue") is not None and fm.get("net_income") is not None
               and fm.get("shares_outstanding") else "sec_partial" if fm else "none")
        r = ScreenResult(sec.id, sec.ticker, "eligible", None, m, fm.get("market_cap"), cov,
                         market_cap_info=mc.to_dict() if merged else None)
        r.completeness = sum(1 for k in COMPLETENESS_KEYS if m.get(k) is not None) / len(COMPLETENESS_KEYS)
        r.financial_score = financial_leader_score(m)
        r.freshness = data_freshness(m.get("stale_sessions"), None)
        r.status, r.reason = _eligibility(sec, m, cov, catalysts.get(sec.id, 0.0), s)
        r.codes = eligibility_codes(sec, m, cov, catalysts.get(sec.id, 0.0), s)
        bm, method_ok = screen_valuation_method(sec, m)
        m["business_model"], m["valuation_method_available"] = bm, method_ok
        results.append(r)

    if enrich_sic:
        _enrich_sectors(session, mode, [by_id[r.security_id] for r in results
                                        if r.status in ("eligible", "catalyst_eligible")], progress)
    sector_relative_valuation(results, {x.id: x.sector for x in secs})
    return results


EXCLUSION_CODES = {
    "foreign_issuer": "Foreign issuer requiring another provider",
    "insufficient_price_history": "Insufficient price history",
    "stale_price": "Stale stock price",
    "invalid_identity": "Invalid company identity (no SEC filer)",
    "insufficient_financial_data": "Insufficient financial data",
    "unknown_market_cap": "Unknown market capitalization",
    "below_min_market_cap": "Market cap below minimum",
    "insufficient_liquidity": "Insufficient liquidity",
    "implausible_financial_data": "Implausible financial data",
    "unsupported_valuation": "Unsupported industry-specific valuation",
}


def eligibility_codes(sec: Security, m: dict[str, Any], cov: str, catalyst: float, s) -> list[str]:
    """Every failing check (a stock can be both illiquid and missing net income), not only the first."""
    codes: list[str] = []
    if sec.security_type in ADR_TYPES and cov in ("foreign", "none"):
        codes.append("foreign_issuer")
    if m.get("sessions", 0) < s.screen_min_price_sessions:
        codes.append("insufficient_price_history")
    if (m.get("stale_sessions") or 0) > s.screen_max_price_staleness:
        codes.append("stale_price")
    if not getattr(sec, "cik", None) and sec.security_type not in ADR_TYPES:
        codes.append("invalid_identity")
    if cov != "foreign" and (m.get("revenue") is None or m.get("net_income") is None):
        codes.append("insufficient_financial_data")
    info = m.get("market_cap_info") or {}
    if m.get("market_cap") is None and cov != "foreign":
        codes.append("unknown_market_cap")
        if "inconsistent_share_counts" in (info.get("flags") or []):
            codes.append("implausible_financial_data")
    elif m.get("market_cap") is not None and m["market_cap"] < s.screen_min_market_cap and not (
            catalyst >= s.catalyst_min_materiality and m["market_cap"] >= s.catalyst_min_market_cap):
        codes.append("below_min_market_cap")
    adv = m.get("avg_dollar_volume_20d")
    if adv is None or adv < s.min_avg_dollar_volume:
        exceptional = catalyst >= s.catalyst_min_materiality and (adv or 0) >= s.catalyst_min_avg_dollar_volume
        if not exceptional:
            codes.append("insufficient_liquidity")
    rev, ni = m.get("revenue"), m.get("net_income")
    if ((rev is not None and rev <= 0) or (rev and rev > 0 and ni is not None and ni > 3 * rev)
            or m.get("financial_status") == "invalid"):
        codes.append("implausible_financial_data")
    return list(dict.fromkeys(codes))


def screen_valuation_method(sec: Security, m: dict[str, Any]) -> tuple[str, bool]:
    """Business model and whether its valuation method has the inputs it needs at the screening stage
    (banks/insurers need equity, REITs net income for FFO; deep analysis verifies the rest)."""
    bm = business_model(sec.sic_code, sec.sector, sec.industry)
    if bm in ("bank", "insurance"):
        return bm, (m.get("equity") or 0) > 0 and m.get("net_income") is not None
    if bm in ("reit", "real_estate"):
        return bm, m.get("net_income") is not None
    return bm, m.get("revenue") is not None and (m.get("net_income") is not None or (m.get("revenue") or 0) > 0)


def _eligibility(sec: Security, m: dict[str, Any], cov: str, catalyst: float, s) -> tuple[str, str | None]:
    if sec.security_type in ADR_TYPES and cov in ("foreign", "none"):
        return "needs_alt_fundamentals", "ADR/foreign issuer: needs an alternative fundamentals provider"
    if m.get("sessions", 0) < s.screen_min_price_sessions:
        return "ineligible", f"insufficient price history ({m.get('sessions', 0)} < {s.screen_min_price_sessions} sessions)"
    if (m.get("stale_sessions") or 0) > s.screen_max_price_staleness:
        return "ineligible", f"stale price ({m['stale_sessions']} sessions old)"
    if cov == "none":
        return "ineligible", "no usable SEC financial data"
    if m.get("revenue") is None or m.get("net_income") is None:
        return "ineligible", "insufficient financial data (revenue or net income not reported)"
    mcap, adv = m.get("market_cap"), m.get("avg_dollar_volume_20d")
    if mcap is None:
        why = (m.get("market_cap_info") or {}).get("reason") or "no shares outstanding"
        return "ineligible", f"market cap unknown ({why})"
    if adv is None:
        return "ineligible", "liquidity unknown (no volume data)"
    big = mcap >= s.screen_min_market_cap and adv >= s.min_avg_dollar_volume
    if big:
        return "eligible", None
    exceptional = (catalyst >= s.catalyst_min_materiality and mcap >= s.catalyst_min_market_cap
                   and adv >= s.catalyst_min_avg_dollar_volume)
    if exceptional:
        return "catalyst_eligible", "below size/liquidity minimum; qualified through an exceptional catalyst"
    if mcap < s.screen_min_market_cap:
        return "ineligible", f"market cap {mcap / 1e6:,.0f}M below {s.screen_min_market_cap / 1e6:,.0f}M"
    return "ineligible", f"illiquid (20d avg dollar volume {adv / 1e6:,.1f}M below {s.min_avg_dollar_volume / 1e6:,.0f}M)"


def _enrich_sectors(session: Session, mode: DataMode, secs: list[Security], progress) -> None:
    """SIC/sector from SEC submissions for screened-in securities that don't have one yet."""
    if mode == "demo":
        return
    from app.providers.sec.edgar import SUBMISSIONS_URL, pad_cik
    from app.services.providers import edgar_client

    todo = [x for x in secs if not x.sic_code and x.cik][: get_settings().sic_enrich_per_run]
    client = edgar_client().client
    for n, sec in enumerate(todo, 1):
        if n % 100 == 1:
            progress(f"Sector classification ({n}/{len(todo)})")
        try:  # no response cache: these documents are large and only the SIC is kept
            data = client.get_json(SUBMISSIONS_URL.format(cik=pad_cik(sec.cik)))
        except ProviderError:
            continue
        classify_from_sic(sec, data.get("sic") or None, data.get("sicDescription") or None)
        sec.country = data.get("stateOfIncorporation") or sec.country
        if n % 200 == 0:
            session.commit()
    session.commit()


def persist_screen(session: Session, results: list[ScreenResult]) -> None:
    now = datetime.now(UTC)
    by_id = {x.id: x for x in session.scalars(select(Security).where(Security.id.in_([r.security_id for r in results])))}
    for r in results:
        sec = by_id[r.security_id]
        if r.status in ("eligible", "catalyst_eligible") and sec.first_eligible_at is None:
            sec.first_eligible_at = now
        sec.eligibility_status, sec.exclusion_reason = r.status, r.reason
        sec.exclusion_codes = r.codes if r.status not in ("eligible", "catalyst_eligible") else []
        sec.market_cap = r.market_cap
        sec.market_cap_basis = ((r.market_cap_info or {}).get("source") or "sec") if r.market_cap else None
        sec.financial_coverage = r.coverage
        sec.screen_metrics = _json_safe({**r.metrics, "financial_score": r.financial_score,
                                         "valuation_score": r.valuation_score, "completeness": r.completeness})
        sec.screened_at = now
    session.commit()


def coverage_stats(results: list[ScreenResult]) -> dict[str, Any]:
    """Market-wide data coverage. Unknown values are counted as unknown, never as zero."""
    from collections import Counter

    m = [r.metrics for r in results]
    srcs = Counter((r.market_cap_info or {}).get("source") for r in results if r.market_cap is not None)
    return {
        "screened": len(results),
        "valid_market_cap": sum(1 for r in results if r.market_cap is not None),
        "market_cap_sources": {k or "unknown": v for k, v in srcs.items()},
        "with_revenue": sum(1 for x in m if x.get("revenue") is not None),
        "with_net_income": sum(1 for x in m if x.get("net_income") is not None),
        "with_cash_flow": sum(1 for x in m if x.get("free_cash_flow") is not None),
        "valid_valuation_method": sum(1 for x in m if x.get("valuation_method_available")),
        "eligible": sum(1 for r in results if r.status in ("eligible", "catalyst_eligible")),
        "requiring_alternative_provider": sum(1 for r in results if "foreign_issuer" in r.codes),
        "companyfacts_fallback_used": sum(1 for x in m if (x.get("fundamentals_source") or "").endswith("companyfacts")),
        "exclusion_codes": dict(Counter(c for r in results if r.status not in ("eligible", "catalyst_eligible")
                                        for c in r.codes).most_common()),
    }


def _json_safe(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in d.items()}


def size_bucket(mcap: float | None) -> str | None:
    if mcap is None:
        return None
    return ("mega" if mcap >= 200e9 else "large" if mcap >= 10e9 else "mid" if mcap >= 2e9
            else "small" if mcap >= 300e6 else "micro")


