"""Screening data coverage: market-wide splits, the SEC companyfacts fallback, provider share counts
and market-capitalization resolution.

Market cap = price per share x current shares outstanding, with the share count taken from the
first valid source in this order:

  1. market-data provider (Massive ticker details; licensed, class-aware) — refreshed in small
     batches because the free tier allows 5 requests/minute
  2. SEC cover-page shares outstanding (dei:EntityCommonStockSharesOutstanding, frames API)
  3. SEC balance-sheet common shares outstanding (us-gaap:CommonStockSharesOutstanding, frames API)
  4. the latest SEC filing's shares outstanding (companyfacts fallback)

Weighted-average diluted shares are never used as a substitute (they measure a past period's
average, for EPS). SEC counts dated before a later split are put on the post-split basis (prices
are split-adjusted). For a company with several listed share classes, an SEC total-share count is
only used when the classes trade at similar prices; otherwise the market cap is unknown until the
provider's class-aware count is available. Nothing is estimated beyond this: an unresolvable market
cap is returned as unknown (None) with the reason.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import DataMode, get_settings
from app.models import MarketSplit, Security, SecurityFundamentals
from app.services.http import ProviderError, ProviderNotConfigured

log = logging.getLogger(__name__)

STALE_SHARES_DAYS = 200
MAX_SHARES_AGE_DAYS = 400
MULTI_CLASS_PRICE_TOLERANCE = 1.25
SHARE_SOURCES = (
    ("provider", "market-data provider shares outstanding (Massive) x price"),
    ("shares_outstanding", "SEC cover-page shares outstanding (dei) x price"),
    ("shares_outstanding_bs", "SEC balance-sheet common shares outstanding x price"),
    ("fallback", "latest SEC filing shares outstanding (companyfacts) x price"),
)


# ------------------------------------------------------------------------------------ splits
def refresh_market_splits(session: Session, mode: DataMode, lookback_days: int = 450,
                          max_age: timedelta = timedelta(hours=20)) -> dict[str, Any]:
    stats: dict[str, Any] = {"requests": 0, "splits": 0}
    last = session.scalar(select(func.max(MarketSplit.fetched_at)).where(MarketSplit.data_mode == mode))
    if last and datetime.now(UTC) - last < max_age:
        stats["skipped"] = "fresh"
        return stats
    from app.services.providers import market_provider

    try:
        provider = market_provider(mode)
        rows = provider.list_splits(date.today() - timedelta(days=lookback_days))
    except (ProviderNotConfigured, ProviderError) as exc:
        stats["error"] = exc.message
        return stats
    stats["requests"] = 1
    now = datetime.now(UTC)
    uniq = {(t, a.ex_date): a for t, a in rows}  # the provider can list the same split more than once
    vals = [dict(data_mode=mode, ticker=t, execution_date=d, split_from=a.split_from, split_to=a.split_to,
                 fetched_at=now) for (t, d), a in uniq.items()]
    if vals:
        stmt = insert(MarketSplit).values(vals)
        session.execute(stmt.on_conflict_do_update(constraint="uq_market_split", set_={"fetched_at": now}))
    session.commit()
    stats["splits"] = len(vals)
    return stats


def splits_by_ticker(session: Session, mode: DataMode) -> dict[str, list[tuple[date, float]]]:
    out: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for t, d, f, to in session.execute(select(MarketSplit.ticker, MarketSplit.execution_date, MarketSplit.split_from,
                                              MarketSplit.split_to).where(MarketSplit.data_mode == mode)):
        if f and to:
            out[t].append((d, to / f))
    return dict(out)


def split_adjust_shares(shares: float, as_of: date | None, splits: list[tuple[date, float]] | None) -> tuple[float, float]:
    """(adjusted shares, factor) for a share count dated `as_of` given later splits."""
    k = 1.0
    if as_of is not None:
        for ex, ratio in splits or []:
            if ex > as_of and ratio > 0:
                k *= ratio
    return shares * k, k


# ------------------------------------------------------------------------------------ market cap
@dataclass
class MarketCap:
    value: float | None
    basis: str | None
    source: str | None
    shares: float | None = None
    as_of: str | None = None
    flags: list[str] = field(default_factory=list)  # e.g. stale_shares, split_adjusted, multi_class
    reason: str | None = None  # why unknown

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "basis": self.basis, "source": self.source, "shares": self.shares,
                "as_of": self.as_of, "flags": self.flags, "reason": self.reason}


def _d(x: Any) -> date | None:
    if not x:
        return None
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def resolve_market_cap(price: float | None, metrics: dict[str, Any], fallback: dict[str, Any] | None,
                       provider_shares: float | None, provider_as_of: datetime | None,
                       splits: list[tuple[date, float]] | None, class_prices: list[float] | None,
                       today: date | None = None) -> MarketCap:
    today = today or date.today()
    if not price or price <= 0:
        return MarketCap(None, None, None, reason="no current price")
    cands: list[tuple[str, float, date | None]] = []
    if provider_shares and provider_shares > 0:
        cands.append(("provider", provider_shares, provider_as_of.date() if provider_as_of else None))
    for key in ("shares_outstanding", "shares_outstanding_bs"):
        m = metrics.get(key) or {}
        if m.get("val"):
            cands.append((key, float(m["val"]), _d(m.get("end"))))
    fb = (fallback or {}).get("shares_outstanding") or {}
    if fb.get("val"):
        cands.append(("fallback", float(fb["val"]), _d(fb.get("end"))))
    if not cands:
        return MarketCap(None, None, None, reason="no shares-outstanding data from the provider or SEC")

    # adjust SEC counts for later splits; drop counts that are too old
    adj: dict[str, tuple[float, date | None, list[str]]] = {}
    for src, sh, d in cands:
        flags: list[str] = []
        if src != "provider" and d is not None:
            age = (today - d).days
            if age > MAX_SHARES_AGE_DAYS:
                continue
            if age > STALE_SHARES_DAYS:
                flags.append("stale_shares")
        sh2, k = split_adjust_shares(sh, d, splits)
        if k != 1.0:
            flags.append(f"split_adjusted_x{k:g}")
        adj[src] = (sh2, d, flags)
    if not adj:
        return MarketCap(None, None, None, reason=f"shares outstanding older than {MAX_SHARES_AGE_DAYS} days")

    # SEC sources must agree with each other (units / class errors)
    sec_vals = [v[0] for k, v in adj.items() if k in ("shares_outstanding", "shares_outstanding_bs")]
    if len(sec_vals) == 2 and not 0.5 <= sec_vals[0] / sec_vals[1] <= 2.0:
        return MarketCap(None, None, None, flags=["inconsistent_share_counts"],
                         reason="SEC share counts inconsistent across concepts (>2x apart)")

    multi = class_prices is not None and len(class_prices) > 1
    for src, label in SHARE_SOURCES:
        if src not in adj:
            continue
        sh, d, flags = adj[src]
        if multi and src != "provider":
            lo, hi = min(class_prices), max(class_prices)
            if lo <= 0 or hi / lo > MULTI_CLASS_PRICE_TOLERANCE:
                return MarketCap(None, None, None, flags=["multi_class"],
                                 reason="several listed share classes at different prices; needs the provider's "
                                        "class-aware share count")
            flags = flags + ["multi_class"]
            label += " (all classes; listed classes trade at similar prices)"
        return MarketCap(price * sh, label, src, sh, d.isoformat() if d else None, flags)
    return MarketCap(None, None, None, reason="no usable share count")


# ------------------------------------------------------------------------------------ companyfacts fallback
def fallback_metrics(companyfacts: dict[str, Any], splits: list[tuple[date, float]] | None = None) -> dict[str, Any]:
    """Screening metrics from one filer's companyfacts (TTM, point-in-time normalizer), with provenance."""
    from app.providers.sec.xbrl import detect_reporting_currency, normalize_companyfacts
    from app.services.financials import build_snapshot

    facts = normalize_companyfacts(companyfacts)
    snap = build_snapshot(facts, currency=detect_reporting_currency(companyfacts), splits=splits)
    if snap.currency != "USD":
        return {"_status": "non_usd", "_currency": snap.currency}
    t = snap.ttm
    out: dict[str, Any] = {}

    def put(key: str, val: float | None, prov_key: str | None, basis: str) -> None:
        if val is None:
            return
        p = snap.provenance.get(prov_key or key) or {}
        out[key] = {"val": val, "end": p.get("period_end"), "accn": p.get("accession"), "concept": p.get("concept"),
                    "period": p.get("basis", basis), "source": "companyfacts"}

    put("revenue", t.get("revenue"), "revenue", "ttm")
    put("revenue_prior", snap.ttm_prior.get("revenue"), "revenue", "ttm_prior")
    put("net_income", t.get("net_income"), "net_income", "ttm")
    put("net_income_prior", snap.ttm_prior.get("net_income"), "net_income", "ttm_prior")
    put("operating_income", t.get("operating_income"), "operating_income", "ttm")
    put("operating_cash_flow", t.get("operating_cash_flow"), "operating_cash_flow", "ttm")
    put("capex", t.get("capex_total"), "capex", "ttm")
    put("cash", t.get("cash_and_investments"), "cash", "instant")
    put("long_term_debt", t.get("total_debt"), "long_term_debt", "instant")
    put("equity", t.get("book_value"), "book_value", "instant")
    put("shares_outstanding", t.get("shares_outstanding"), "shares_outstanding", "instant")
    out["_status"] = snap.financial_status
    out["_validation"] = snap.validation[:3]
    return out


def refresh_fundamentals_fallback(session: Session, mode: DataMode, progress: Callable[[str], None] = lambda m: None,
                                  budget: int | None = None, max_seconds: float = 900.0) -> dict[str, Any]:
    """SEC companyfacts for liquid securities the frames API left without revenue, net income or shares.
    Bounded per run (count and time) and refreshed at most every FUNDAMENTALS_FALLBACK_MAX_AGE_DAYS."""
    s = get_settings()
    budget = s.fundamentals_fallback_per_run if budget is None else budget
    stats: dict[str, Any] = {"requests": 0, "filled": 0, "errors": 0, "candidates": 0}
    if mode == "demo" or budget <= 0:
        stats["skipped"] = "demo mode" if mode == "demo" else "disabled"
        return stats
    from app.providers.sec.edgar import COMPANYFACTS_URL, pad_cik
    from app.services.providers import edgar_client

    funds = {f.cik: f for f in session.scalars(select(SecurityFundamentals).where(SecurityFundamentals.data_mode == mode))}
    cutoff = datetime.now(UTC) - timedelta(days=s.fundamentals_fallback_max_age_days)
    secs = session.scalars(select(Security).where(
        Security.data_mode == mode, Security.active, Security.cik.is_not(None),
        Security.security_type.in_(("CS", "OS")))).all()
    todo: dict[str, float] = {}
    for sec in secs:
        adv = (sec.screen_metrics or {}).get("avg_dollar_volume_20d") or 0
        if adv < s.fundamentals_fallback_min_dollar_volume:
            continue
        f = funds.get(sec.cik)
        m = (f.metrics if f else None) or {}
        missing = [k for k in ("revenue", "net_income", "shares_outstanding") if k not in m]
        if not missing and sec.market_cap is not None:
            continue
        if f is not None and f.fallback_fetched_at and f.fallback_fetched_at > cutoff:
            continue
        todo[sec.cik] = max(todo.get(sec.cik, 0), adv)
    order = sorted(todo, key=lambda c: (-todo[c], c))[:budget]
    stats["candidates"] = len(todo)
    split_map = splits_by_ticker(session, mode)
    tick_by_cik: dict[str, str] = {}
    for sec in secs:
        tick_by_cik.setdefault(sec.cik, sec.ticker)
    client = edgar_client().client
    t0 = time.monotonic()
    for n, cik in enumerate(order, 1):
        if time.monotonic() - t0 > max_seconds:
            stats["stopped"] = f"time budget {max_seconds:.0f}s reached"
            break
        if n % 25 == 1:
            progress(f"SEC companyfacts fallback ({n}/{len(order)})")
        try:  # no response cache: these documents are large; only the extracted metrics are stored
            data = client.get_json(COMPANYFACTS_URL.format(cik=pad_cik(cik)))
            stats["requests"] += 1
            fb = fallback_metrics(data, split_map.get(tick_by_cik.get(cik, ""), []))
        except ProviderError as exc:
            stats["errors"] += 1
            fb = {"_status": "error", "_error": exc.message[:200]}
        except Exception as exc:  # malformed filings must not stop the batch
            log.warning("companyfacts fallback failed for %s: %s", cik, exc)
            stats["errors"] += 1
            fb = {"_status": "error", "_error": f"{type(exc).__name__}"}
        if any(k in fb for k in ("revenue", "net_income", "shares_outstanding")):
            stats["filled"] += 1
        stmt = insert(SecurityFundamentals).values(cik=cik, data_mode=mode, metrics={}, fallback=fb,
                                                   fallback_fetched_at=datetime.now(UTC), fetched_at=datetime.now(UTC))
        session.execute(stmt.on_conflict_do_update(index_elements=["cik", "data_mode"], set_={
            "fallback": stmt.excluded.fallback, "fallback_fetched_at": stmt.excluded.fallback_fetched_at}))
        if n % 25 == 0:
            session.commit()
    session.commit()
    stats["seconds"] = round(time.monotonic() - t0, 1)
    return stats


def merged_fundamentals(frames: dict[str, Any] | None, fallback: dict[str, Any] | None) -> dict[str, Any] | None:
    """Frames metrics first (uniform, bulk); companyfacts fills only what frames lacked."""
    if not frames and not fallback:
        return None
    out = dict(frames or {})
    for k, v in (fallback or {}).items():
        if not k.startswith("_") and k not in out:
            out[k] = v
    return out


# ------------------------------------------------------------------------------------ provider share counts
def refresh_provider_shares(session: Session, mode: DataMode, progress: Callable[[str], None] = lambda m: None,
                            budget: int | None = None) -> dict[str, Any]:
    """Massive ticker details (class-aware shares outstanding) for liquid securities whose market cap is
    unknown — in small batches (free tier: 5 requests/minute)."""
    s = get_settings()
    budget = s.provider_shares_per_run if budget is None else budget
    stats: dict[str, Any] = {"requests": 0, "filled": 0, "errors": 0}
    if budget <= 0:
        return stats
    from app.services.providers import market_provider

    try:
        provider = market_provider(mode)
    except ProviderNotConfigured as exc:
        stats["error"] = exc.message
        return stats
    cutoff = datetime.now(UTC) - timedelta(days=s.provider_shares_max_age_days)
    rows = session.scalars(select(Security).where(
        Security.data_mode == mode, Security.active, Security.security_type.in_(("CS", "OS")),
        Security.market_cap.is_(None),
        or_(Security.provider_checked_at.is_(None), Security.provider_checked_at < cutoff))).all()
    rows = [r for r in rows if ((r.screen_metrics or {}).get("avg_dollar_volume_20d") or 0) >= s.min_avg_dollar_volume]
    rows.sort(key=lambda r: (-((r.screen_metrics or {}).get("avg_dollar_volume_20d") or 0), r.ticker))
    stats["candidates"] = len(rows)
    for n, sec in enumerate(rows[:budget], 1):
        progress(f"Provider share counts ({n}/{min(budget, len(rows))})")
        try:
            p = provider.get_company_profile(sec.ticker)
            stats["requests"] += 1
            sec.provider_shares = p.shares_outstanding
            sec.provider_market_cap = p.market_cap
            stats["filled"] += bool(p.shares_outstanding)
        except ProviderError as exc:
            stats["errors"] += 1
            log.info("provider profile failed for %s: %s", sec.ticker, exc.message)
        sec.provider_checked_at = datetime.now(UTC)
        session.commit()
    return stats
