"""Master stock universe, discovered from the market-data provider (not a hand-made list).

* Paginates the provider's ticker reference list for each configured security type.
* Identity = composite FIGI when available (stable across ticker changes), else CIK+ticker.
* Ticker changes keep the same security (and its linked company, so historical forecasts stay
  attached); delistings mark rows inactive instead of deleting them; relistings reactivate.
* Types outside the valuation pipeline (ETFs, funds, warrants, rights, preferreds...) and
  exchanges outside the configured set are kept for counts/audit but marked excluded.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import DataMode, get_settings
from app.models import Company, Security, SecurityTickerHistory
from app.providers.market.base import ListedSecurity
from app.services.forecasting.sectors import sector_from_sic
from app.services.http import ProviderError
from app.services.providers import market_provider

log = logging.getLogger(__name__)

PIPELINE_TYPES = {"CS", "OS"}  # standard valuation pipeline
ADR_TYPES = {"ADRC"}  # discovered; fundamentals usually need an alternative provider
MIN_ACTIVE_RATIO_FOR_DELISTING = 0.5  # guard against partial refreshes mass-delisting


def security_key(s: ListedSecurity) -> str:
    if s.composite_figi:
        return f"FIGI:{s.composite_figi}"
    if s.cik:
        return f"CIK:{s.cik}:{s.ticker}"
    return f"TKR:{s.ticker}"


def type_status(s: ListedSecurity, exchanges: set[str]) -> tuple[str, str | None]:
    """Initial status from listing metadata (screening refines 'unscreened' later)."""
    if not s.active:
        return "inactive", "not actively listed"
    if s.security_type not in PIPELINE_TYPES | ADR_TYPES:
        return "excluded_type", f"security type {s.security_type} is outside the stock valuation pipeline"
    if s.exchange not in exchanges:
        return "excluded_type", f"primary exchange {s.exchange or 'unknown'} not in {sorted(exchanges)}"
    if s.security_type in ADR_TYPES:
        return "needs_alt_fundamentals", "ADR: SEC CompanyFacts usually unavailable; needs alternative fundamentals"
    return "unscreened", None


def refresh_master_universe(session: Session, mode: DataMode, progress: Callable[[str], None] = lambda m: None,
                            listings: list[ListedSecurity] | None = None) -> dict[str, Any]:
    """Refresh the master universe. `listings` may be injected (tests); otherwise fetched."""
    s = get_settings()
    exchanges = set(s.universe_exchanges)
    now = datetime.now(UTC)
    stats: dict[str, Any] = {"pages": 0, "errors": []}
    complete = True
    if listings is None:
        listings = []
        provider = market_provider(mode)
        for stype in s.universe_security_types:
            progress(f"Listing {stype} securities")
            try:
                rows = provider.list_securities(stype, on_page=lambda n: stats.__setitem__("pages", stats["pages"] + 1))
                listings.extend(rows)
            except ProviderError as exc:
                complete = False
                stats["errors"].append(f"{stype}: {exc.message}")
    stats["listed"] = len(listings)

    existing = list(session.scalars(select(Security).where(Security.data_mode == mode)))
    by_key = {x.security_key: x for x in existing}
    active_by_ticker = {x.ticker: x for x in existing if x.active}
    companies = {c.ticker: c for c in session.scalars(select(Company).where(Company.data_mode == mode))}
    seen: set[int] = set()
    counts: Counter[str] = Counter()

    for ls in listings:
        key = security_key(ls)
        sec = by_key.get(key)
        if sec is None:
            other = active_by_ticker.get(ls.ticker)
            if other is not None and other.id not in seen and other.cik and other.cik == ls.cik:
                sec = other  # same filer gained a FIGI (or key format changed): same identity
                sec.security_key = key
                by_key[key] = sec
            elif other is not None and other.id not in seen:
                _delist(session, other, now, "ticker reassigned to a different security")
                counts["ticker_reassigned"] += 1
        if sec is None:
            sec = Security(data_mode=mode, security_key=key, ticker=ls.ticker, provider="demo" if mode == "demo" else "massive",
                           first_seen_at=now)
            session.add(sec)
            session.flush()
            session.add(SecurityTickerHistory(security_id=sec.id, old_ticker=None, new_ticker=ls.ticker, change_type="listed"))
            by_key[key] = sec
            counts["new"] += 1
        elif sec.ticker != ls.ticker:
            session.add(SecurityTickerHistory(security_id=sec.id, old_ticker=sec.ticker, new_ticker=ls.ticker,
                                              change_type="ticker_change"))
            _rename_company(session, sec, ls.ticker, companies)
            sec.ticker = ls.ticker
            counts["ticker_changes"] += 1
        if not sec.active and ls.active:
            session.add(SecurityTickerHistory(security_id=sec.id, old_ticker=sec.ticker, new_ticker=ls.ticker,
                                              change_type="relisted"))
            sec.delisted_at = None
            counts["relisted"] += 1
        sec.name, sec.exchange, sec.security_type = ls.name, ls.exchange, ls.security_type
        sec.active, sec.currency, sec.locale = ls.active, ls.currency, ls.locale
        sec.cik = ls.cik or sec.cik
        sec.composite_figi, sec.share_class_figi = ls.composite_figi, ls.share_class_figi
        sec.provider_updated_at, sec.last_seen_at, sec.last_refreshed_at = ls.last_updated, now, now
        status, reason = type_status(ls, exchanges)
        if status != "unscreened" or sec.eligibility_status in (None, "inactive", "excluded_type"):
            sec.eligibility_status, sec.exclusion_reason = status, reason
        if sec.company_id is None and ls.ticker in companies:
            sec.company_id = companies[ls.ticker].id
        if mode == "demo" and not sec.sic_code:
            from app.demo.synthetic import demo_classification

            sector, sic, industry = demo_classification(ls.ticker)
            sec.sic_code, sec.industry, sec.sector = sic, industry, sector
        seen.add(sec.id)
        counts[f"type_{ls.security_type}"] += 1

    # Delistings: previously active securities of fetched types that no longer appear.
    prev_active = [x for x in existing if x.active and x.security_type in set(s.universe_security_types)]
    if complete and listings and len(seen) >= MIN_ACTIVE_RATIO_FOR_DELISTING * max(1, len(prev_active)):
        for x in prev_active:
            if x.id not in seen:
                _delist(session, x, now, "no longer in the provider's active listings")
                counts["delisted"] += 1
    elif prev_active:
        stats["errors"].append("delisting pass skipped: refresh incomplete or suspiciously small")

    session.commit()
    stats.update(counts)
    stats["active_total"] = session.query(Security).filter(Security.data_mode == mode, Security.active).count()
    stats["by_status"] = dict(Counter(st for (st,) in session.execute(
        select(Security.eligibility_status).where(Security.data_mode == mode)).all()))
    stats["version"] = universe_version(session, mode)
    return stats


def _delist(session: Session, sec: Security, now: datetime, reason: str) -> None:
    sec.active = False
    sec.delisted_at = now
    sec.eligibility_status, sec.exclusion_reason = "inactive", reason
    session.add(SecurityTickerHistory(security_id=sec.id, old_ticker=sec.ticker, new_ticker=sec.ticker,
                                      change_type="delisted"))


def _rename_company(session: Session, sec: Security, new_ticker: str, companies: dict[str, Company]) -> None:
    """Keep the analysis/forecast history attached to the same company row after a ticker change."""
    if sec.company_id is None:
        return
    comp = session.get(Company, sec.company_id)
    if comp is None or new_ticker in companies:
        return
    companies.pop(comp.ticker, None)
    comp.ticker = new_ticker
    companies[new_ticker] = comp


def universe_version(session: Session, mode: DataMode) -> str:
    """Hash of the active universe membership (changes whenever a listing is added/removed)."""
    keys = sorted(k for (k,) in session.execute(
        select(Security.security_key).where(Security.data_mode == mode, Security.active)).all())
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()[:16]


def classify_from_sic(sec: Security, sic: str | None, description: str | None) -> None:
    if sic:
        sec.sic_code, sec.industry = sic, description
        sec.sector = sector_from_sic(sic, description)


def company_for_security(session: Session, sec: Security) -> Company:
    """Get or create the analysis company row for a master-universe security (links both ways)."""
    from app.services.analysis import get_or_create_company

    if sec.company_id:
        comp = session.get(Company, sec.company_id)
        if comp is not None:
            return comp
    comp = get_or_create_company(session, sec.ticker, sec.data_mode)
    comp.name = comp.name or sec.name
    comp.cik = comp.cik or sec.cik
    comp.exchange = comp.exchange or sec.exchange
    comp.sic_code = comp.sic_code or sec.sic_code
    comp.industry = comp.industry or sec.industry
    comp.sector = comp.sector or sec.sector
    sec.company_id = comp.id
    return comp


def make_resolver(session: Session, mode: DataMode):
    """Resolver for event ingestion: any active, pipeline-type security can be linked to news,
    including stocks never monitored or analyzed before."""
    secs = {x.ticker: x for x in session.scalars(select(Security).where(
        Security.data_mode == mode, Security.active, Security.security_type.in_(PIPELINE_TYPES | ADR_TYPES)))}

    def resolve(ticker: str) -> Company | None:
        sec = secs.get(ticker) or secs.get(ticker.replace("-", "."))
        return company_for_security(session, sec) if sec is not None else None

    return resolve, set(secs)
