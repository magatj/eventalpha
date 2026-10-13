"""Provider factory + runtime data-mode setting. The rest of the app asks for providers
here, so demo and live implementations are swapped in one place."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.config import DataMode, get_settings
from app.demo.synthetic import DemoMarketProvider, demo_articles, demo_financial_facts, demo_upcoming_events
from app.models import AppSetting
from app.providers.market.base import MarketDataProvider
from app.providers.market.massive import MassiveProvider
from app.providers.news.base import NewsProvider, RawArticle, UpcomingEvent
from app.providers.news.live import LiveNewsProvider
from app.providers.news.rss import RssNewsProvider
from app.providers.sec.edgar import EdgarClient
from app.providers.sec.xbrl import FinancialFact, normalize_companyfacts
from app.services.http import DbCache, ProviderNotConfigured

_db_cache = DbCache()


def get_data_mode(session: Session) -> DataMode:
    row = session.get(AppSetting, "data_mode")
    if row and row.value in ("demo", "live"):
        return row.value
    return get_settings().data_mode


def set_data_mode(session: Session, mode: DataMode) -> None:
    row = session.get(AppSetting, "data_mode")
    if row:
        row.value = mode
    else:
        session.add(AppSetting(key="data_mode", value=mode))


def market_provider(mode: DataMode) -> MarketDataProvider:
    if mode == "demo":
        return DemoMarketProvider()
    return MassiveProvider(get_settings(), cache=_db_cache)


_edgar: EdgarClient | None = None


def edgar_client() -> EdgarClient:
    global _edgar
    if _edgar is None:
        _edgar = EdgarClient(get_settings(), cache=_db_cache)
    return _edgar


class FinancialDataSource:
    def __init__(self, mode: DataMode):
        self.mode = mode

    def facts(self, ticker: str, cik: str | None) -> tuple[list[FinancialFact], dict]:
        """Return (facts, meta). meta carries currency and source references."""
        if self.mode == "demo":
            return demo_financial_facts(ticker, DemoMarketProvider().as_of), {
                "currency": "USD", "source": "demo_synthetic", "url": None}
        client = edgar_client()
        if not cik:
            cik = client.resolve_ticker(ticker).cik
        data = client.companyfacts(cik)
        from app.providers.sec.xbrl import detect_reporting_currency

        return normalize_companyfacts(data), {
            "currency": detect_reporting_currency(data), "source": "sec_edgar",
            "url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", "entity": data.get("entityName"),
        }


class DemoNewsProvider(NewsProvider):
    name = "demo_synthetic"

    def __init__(self, universe: list[str]):
        self.universe = universe
        self.as_of = DemoMarketProvider().as_of

    def fetch_recent_news(self) -> list[RawArticle]:
        return [RawArticle(**a.model_dump(exclude={"sentiment"})) for a in demo_articles(self.as_of, self.universe)]

    def fetch_company_news(self, ticker: str) -> list[RawArticle]:
        return [RawArticle(**a.model_dump(exclude={"sentiment"})) for a in demo_articles(self.as_of, [ticker])]

    def fetch_upcoming_events(self, tickers: list[str]) -> list[UpcomingEvent]:
        return [UpcomingEvent(**e) for e in demo_upcoming_events(self.as_of, tickers)]

    def fetch_sec_filings(self, ticker: str) -> list[RawArticle]:
        return []


def news_provider(mode: DataMode, universe: list[str] | None = None) -> NewsProvider:
    if mode == "demo":
        return DemoNewsProvider(universe or [])
    s = get_settings()
    try:
        market: MarketDataProvider | None = market_provider("live")
    except ProviderNotConfigured:
        market = None
    try:
        edgar: EdgarClient | None = edgar_client()
    except Exception:
        edgar = None
    rss = RssNewsProvider(s.rss_feeds, s.sec_user_agent, cache=_db_cache) if s.rss_feeds else None
    return LiveNewsProvider(edgar, market, rss)


def today() -> date:
    return date.today()
