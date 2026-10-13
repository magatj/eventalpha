"""Monitored stock universe (configurable list of US-listed tickers)."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import DataMode
from app.demo.synthetic import DEMO_UNIVERSE, DemoMarketProvider
from app.models import Company, WatchlistItem
from app.providers.sec.edgar import normalize_ticker
from app.services.forecasting.sectors import sector_from_sic
from app.services.http import ProviderError

UNIVERSE_FILE = Path(os.environ.get("UNIVERSE_FILE", Path(__file__).resolve().parents[1] / "data" / "universe.txt"))


def universe_tickers(mode: DataMode) -> list[str]:
    if mode == "demo":
        return list(DEMO_UNIVERSE)
    lines = UNIVERSE_FILE.read_text().splitlines()
    return [ln.split("#")[0].strip().upper() for ln in lines if ln.split("#")[0].strip()]


def ensure_universe_companies(session: Session, mode: DataMode, tickers: list[str]) -> dict[str, Company]:
    """Make sure each ticker has a company row with a name (for news association)."""
    from app.services.analysis import get_or_create_company

    out: dict[str, Company] = {}
    ticker_map = None
    if mode == "live":
        try:
            from app.services.providers import edgar_client

            ticker_map = edgar_client().ticker_map()
        except ProviderError:
            ticker_map = None
    demo = DemoMarketProvider() if mode == "demo" else None
    for t in tickers:
        c = get_or_create_company(session, t, mode)
        if not c.name:
            if demo:
                p = demo.get_company_profile(t)
                c.name, c.sic_code, c.industry, c.exchange = p.name, p.sic_code, p.sic_description, p.exchange
                c.sector = sector_from_sic(p.sic_code, p.sic_description)
            elif ticker_map and normalize_ticker(t) in ticker_map:
                ident = ticker_map[normalize_ticker(t)]
                c.name, c.cik, c.exchange = ident.name, ident.cik, ident.exchange
        out[t] = c
    return out


def watchlist_tickers(session: Session, mode: DataMode) -> list[str]:
    return list(session.scalars(select(Company.ticker).join(WatchlistItem, WatchlistItem.company_id == Company.id)
                                .where(Company.data_mode == mode)))
