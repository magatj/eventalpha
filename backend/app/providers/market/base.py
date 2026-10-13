"""Market-data provider abstraction. Business logic depends only on this interface,
so vendors can be swapped without touching analysis/forecasting code."""

from __future__ import annotations

import abc
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class PriceBar(BaseModel):
    date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float
    volume: float | None = None
    vwap: float | None = None


class LatestPrice(BaseModel):
    ticker: str
    price: float
    as_of_date: date
    as_of_timestamp: datetime | None = None
    kind: Literal["previous_close", "last_trade", "stored_close"] = "previous_close"
    source: str
    volume: float | None = None


class CompanyProfile(BaseModel):
    ticker: str
    name: str | None = None
    exchange: str | None = None
    cik: str | None = None
    sic_code: str | None = None
    sic_description: str | None = None
    market_cap: float | None = None
    shares_outstanding: float | None = None
    currency: str | None = None
    active: bool = True
    security_type: str | None = None
    list_date: date | None = None
    source: str


class CorporateActionData(BaseModel):
    action_type: Literal["split", "dividend"]
    ex_date: date
    split_from: float | None = None
    split_to: float | None = None
    cash_amount: float | None = None


class IndexQuote(BaseModel):
    symbol: str
    name: str
    value: float | None
    change_pct: float | None = None
    as_of_date: date | None = None
    kind: Literal["index", "etf_proxy"]
    proxy_for: str | None = None
    source: str
    unavailable_reason: str | None = None


class ProviderArticle(BaseModel):
    """A news item as returned by a market-data vendor's news endpoint."""

    url: str
    title: str
    summary: str | None = None
    source_name: str | None = None
    published_at: datetime | None = None
    tickers: list[str] = Field(default_factory=list)
    provider: str
    sentiment: dict[str, str] = Field(default_factory=dict)  # vendor-provided, informational only


class MarketDataProvider(abc.ABC):
    name: str = "abstract"
    is_demo: bool = False
    coverage_notes: list[str] = []

    @abc.abstractmethod
    def get_latest_price(self, ticker: str) -> LatestPrice: ...

    @abc.abstractmethod
    def get_historical_prices(self, ticker: str, start_date: date, end_date: date) -> list[PriceBar]:
        """Return RAW (split-unadjusted) daily bars, ascending. Adjust with
        app.services.prices.apply_split_adjustment using get_corporate_actions()."""

    @abc.abstractmethod
    def get_company_profile(self, ticker: str) -> CompanyProfile: ...

    @abc.abstractmethod
    def get_corporate_actions(self, ticker: str) -> list[CorporateActionData]: ...

    @abc.abstractmethod
    def get_market_indices(self) -> list[IndexQuote]: ...

    def get_company_news(self, ticker: str, limit: int = 20) -> list[ProviderArticle]:
        return []

    def get_market_news(self, limit: int = 1000) -> list[ProviderArticle]:
        """Most recent market-wide news (all tickers), newest first."""
        return []

    def get_grouped_daily(self, day: date) -> dict[str, PriceBar]:
        """All tickers' bars for one session (used for cheap universe-wide refreshes)."""
        return {}
