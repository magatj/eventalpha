"""Massive (formerly Polygon.io) market-data provider.

Coverage notes for the free "Stocks Basic" plan (verify against your plan):
  * End-of-day data only (no real-time quotes); ~2 years of daily history.
  * 5 API requests / minute — enforced client-side by RateLimiter.
  * Index values (I:SPX, I:NDX, I:DJI) need an Indices plan, so the market overview
    uses SPY / QQQ / DIA ETFs as clearly-labeled proxies.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.providers.market.base import (
    CompanyProfile,
    CorporateActionData,
    IndexQuote,
    LatestPrice,
    ListedSecurity,
    MarketDataProvider,
    PriceBar,
    ProviderArticle,
)
from app.services.http import (
    DataUnavailable,
    HttpClient,
    ProviderAccessDenied,
    ProviderError,
    ProviderNotConfigured,
    RateLimiter,
    ResponseCache,
)
from app.services.market_calendar import to_ny_date

log = logging.getLogger(__name__)

PROVIDER = "massive"

# (display symbol, index name, Massive index ticker if the plan is entitled, FRED series, fallback ETF)
INDEXES = [
    ("SPX", "S&P 500", None, "SP500", "SPY"),  # I:SPX / I:DJI need a paid Massive Indices plan (HTTP 403)
    ("NDX", "Nasdaq-100", "I:NDX", "NASDAQ100", "QQQ"),
    ("DJI", "Dow Jones Industrial Average", None, "DJIA", "DIA"),
]


# --- response validation models -------------------------------------------------
class _Agg(BaseModel):
    t: int
    c: float
    o: float | None = None
    h: float | None = None
    l: float | None = None  # noqa: E741
    v: float | None = None
    vw: float | None = None
    T: str | None = None


class _AggsResponse(BaseModel):
    status: str | None = None
    results: list[_Agg] | None = None
    resultsCount: int | None = None
    next_url: str | None = None


class _TickerDetails(BaseModel):
    ticker: str
    name: str | None = None
    primary_exchange: str | None = None
    cik: str | None = None
    sic_code: str | None = None
    sic_description: str | None = None
    market_cap: float | None = None
    share_class_shares_outstanding: float | None = None
    weighted_shares_outstanding: float | None = None
    currency_name: str | None = None
    active: bool = True
    type: str | None = None
    list_date: date | None = None


class _Split(BaseModel):
    execution_date: date
    split_from: float
    split_to: float


class _Dividend(BaseModel):
    ex_dividend_date: date
    cash_amount: float | None = None


class _Publisher(BaseModel):
    name: str | None = None


class _Insight(BaseModel):
    ticker: str
    sentiment: str | None = None


class _News(BaseModel):
    title: str
    article_url: str
    published_utc: str | None = None
    description: str | None = None
    tickers: list[str] = []
    publisher: _Publisher | None = None
    insights: list[_Insight] | None = None


def _bar(a: _Agg) -> PriceBar:
    return PriceBar(date=to_ny_date(a.t), open=a.o, high=a.h, low=a.l, close=a.c, volume=a.v, vwap=a.vw)


class MassiveProvider(MarketDataProvider):
    name = PROVIDER
    is_demo = False
    coverage_notes = [
        "Free plan: end-of-day prices (previous close), not real-time quotes.",
        "Free plan: about 2 years of daily history.",
        "Free plan: 5 requests/minute; requests are queued client-side.",
        "Index levels are the official closes: S&P 500 and Dow Jones from FRED (St. Louis Fed), Nasdaq-100 from "
        "Massive. FRED publishes a session's close in the evening or the next morning; each tile shows its date.",
    ]

    def __init__(self, settings: Settings, cache: ResponseCache | None = None, client: HttpClient | None = None):
        if not settings.massive_api_key and client is None:
            raise ProviderNotConfigured(PROVIDER, "MASSIVE_API_KEY is not set; live market data is unavailable.")
        self.client = client or HttpClient(
            PROVIDER,
            base_url=settings.massive_base_url,
            # Key in the Authorization header so it never appears in URLs/logs.
            headers={"Authorization": f"Bearer {settings.massive_api_key}", "Accept": "application/json"},
            rate_limiter=_shared_limiter(settings.massive_requests_per_minute),
            timeout=settings.massive_timeout_seconds,
            cache=cache,
        )

    # -- interface ---------------------------------------------------------------
    def get_latest_price(self, ticker: str) -> LatestPrice:
        data = self.client.get_json(
            f"/v2/aggs/ticker/{ticker.upper()}/prev", {"adjusted": "true"}, cache_ttl=timedelta(hours=1)
        )
        resp = self._validate(_AggsResponse, data, "previous close")
        if not resp.results:
            raise DataUnavailable(PROVIDER, f"no previous-close bar for {ticker}")
        a = resp.results[0]
        bar = _bar(a)
        return LatestPrice(ticker=ticker.upper(), price=a.c, as_of_date=bar.date, kind="previous_close",
                           source=PROVIDER, volume=a.v)

    def get_historical_prices(self, ticker: str, start_date: date, end_date: date) -> list[PriceBar]:
        url: str | None = f"/v2/aggs/ticker/{ticker.upper()}/range/1/day/{start_date}/{end_date}"
        params: dict | None = {"adjusted": "false", "sort": "asc", "limit": 50000}
        bars: list[PriceBar] = []
        pages = 0
        while url and pages < 10:
            data = self.client.get_json(url, params, cache_ttl=timedelta(hours=6))
            resp = self._validate(_AggsResponse, data, "aggregates")
            bars.extend(_bar(a) for a in resp.results or [])
            url, params = resp.next_url, None
            pages += 1
        bars.sort(key=lambda b: b.date)
        return bars

    def get_company_profile(self, ticker: str) -> CompanyProfile:
        data = self.client.get_json(f"/v3/reference/tickers/{ticker.upper()}", cache_ttl=timedelta(days=1))
        results = data.get("results") if isinstance(data, dict) else None
        if not results:
            raise DataUnavailable(PROVIDER, f"unknown ticker {ticker}")
        d = self._validate(_TickerDetails, results, "ticker details")
        return CompanyProfile(
            ticker=d.ticker, name=d.name, exchange=d.primary_exchange, cik=d.cik, sic_code=d.sic_code,
            sic_description=d.sic_description, market_cap=d.market_cap,
            shares_outstanding=d.weighted_shares_outstanding or d.share_class_shares_outstanding,
            currency=(d.currency_name or "").upper() or None, active=d.active, security_type=d.type,
            list_date=d.list_date, source=PROVIDER,
        )

    def get_corporate_actions(self, ticker: str) -> list[CorporateActionData]:
        out: list[CorporateActionData] = []
        data = self.client.get_json(
            "/v3/reference/splits", {"ticker": ticker.upper(), "limit": 1000}, cache_ttl=timedelta(days=1)
        )
        for raw in (data or {}).get("results") or []:
            try:
                s = _Split.model_validate(raw)
            except ValidationError:
                continue
            out.append(CorporateActionData(action_type="split", ex_date=s.execution_date,
                                           split_from=s.split_from, split_to=s.split_to))
        try:
            data = self.client.get_json(
                "/v3/reference/dividends", {"ticker": ticker.upper(), "limit": 1000}, cache_ttl=timedelta(days=1)
            )
            for raw in (data or {}).get("results") or []:
                try:
                    dv = _Dividend.model_validate(raw)
                except ValidationError:
                    continue
                out.append(CorporateActionData(action_type="dividend", ex_date=dv.ex_dividend_date,
                                               cash_amount=dv.cash_amount))
        except ProviderAccessDenied as exc:
            log.info("dividends not covered by plan: %s", exc)
        return sorted(out, key=lambda c: c.ex_date)

    def get_market_indices(self) -> list[IndexQuote]:
        """Actual index levels, not ETF prices. Preference per index: Massive index aggregates where the plan
        is entitled (the free plan includes the Nasdaq-100 but not the S&P 500 or Dow), then the official
        close published by FRED, and only if both fail the ETF price, labeled as a proxy."""
        from app.providers.market.fred import FredIndexSource

        fred: dict = {}
        fred_error = None
        try:
            fred = FredIndexSource(cache=self.client.cache).latest([x[3] for x in INDEXES])
        except ProviderError as exc:
            fred_error = exc.message
        quotes: list[IndexQuote] = []
        end = date.today()
        for sym, name, massive_ticker, fred_id, etf in INDEXES:
            q = None
            if massive_ticker:
                try:
                    q = self._index_from_aggs(massive_ticker, name, end, kind="index")
                    q.symbol = sym
                except ProviderError:
                    q = None
            if q is None and fred_id in fred:
                d, v, prev = fred[fred_id]
                q = IndexQuote(symbol=sym, name=name, value=v, change_pct=(v / prev - 1) if prev else None,
                               as_of_date=d, kind="index", source="fred")
            if q is None:  # last resort: the ETF, clearly labeled as a proxy
                try:
                    q = self._index_from_aggs(etf, name, end, kind="etf_proxy")
                    q.unavailable_reason = f"index level unavailable ({fred_error or 'no published close'}); ETF shown"
                except ProviderError as exc:
                    q = IndexQuote(symbol=sym, name=name, value=None, kind="index", source="fred",
                                   unavailable_reason=fred_error or exc.message)
            quotes.append(q)
        return quotes

    def _index_from_aggs(self, ticker: str, name: str, end: date, kind: str) -> IndexQuote:
        data = self.client.get_json(f"/v2/aggs/ticker/{ticker}/range/1/day/{end - timedelta(days=10)}/{end}",
                                    {"adjusted": "true", "sort": "asc"}, cache_ttl=timedelta(hours=3))
        bars = [_bar(a) for a in (self._validate(_AggsResponse, data, "aggregates").results or [])]
        if not bars:
            raise DataUnavailable(PROVIDER, f"no bars for {ticker}")
        last = bars[-1]
        chg = (last.close / bars[-2].close - 1) if len(bars) > 1 else None
        return IndexQuote(symbol=ticker, name=name if kind == "index" else f"{ticker} ETF (proxy)", value=last.close,
                          change_pct=chg, as_of_date=last.date, kind=kind,  # type: ignore[arg-type]
                          proxy_for=name if kind == "etf_proxy" else None, source=PROVIDER)

    def get_company_news(self, ticker: str, limit: int = 20) -> list[ProviderArticle]:
        return self._news({"ticker": ticker.upper(), "limit": limit, "order": "desc", "sort": "published_utc"})

    def get_market_news(self, limit: int = 1000) -> list[ProviderArticle]:
        # One request returns ~1000 articles tagged with tickers across the whole market.
        return self._news({"limit": min(limit, 1000), "order": "desc", "sort": "published_utc"},
                          ttl=timedelta(minutes=50))

    def _news(self, params: dict, ttl: timedelta = timedelta(minutes=60)) -> list[ProviderArticle]:
        data = self.client.get_json("/v2/reference/news", params, cache_ttl=ttl)
        out: list[ProviderArticle] = []
        for raw in (data or {}).get("results") or []:
            try:
                n = _News.model_validate(raw)
            except ValidationError:
                continue
            from dateutil import parser as dtparser

            out.append(ProviderArticle(
                url=n.article_url, title=n.title, summary=n.description,
                source_name=n.publisher.name if n.publisher else None,
                published_at=dtparser.isoparse(n.published_utc) if n.published_utc else None,
                tickers=n.tickers, provider=PROVIDER,
                sentiment={i.ticker: i.sentiment for i in n.insights or [] if i.sentiment},
            ))
        return out

    def list_securities(self, security_type: str, on_page=lambda n: None, max_pages: int = 50) -> list[ListedSecurity]:
        """Paginated /v3/reference/tickers (1000 per page, follows next_url)."""
        from dateutil import parser as dtparser

        url: str | None = "/v3/reference/tickers"
        params: dict | None = {"market": "stocks", "active": "true", "type": security_type, "limit": 1000,
                               "order": "asc", "sort": "ticker"}
        out: list[ListedSecurity] = []
        pages = 0
        while url and pages < max_pages:
            data = self.client.get_json(url, params)
            pages += 1
            for r in (data or {}).get("results") or []:
                if not r.get("ticker"):
                    continue
                lu = r.get("last_updated_utc")
                out.append(ListedSecurity(
                    ticker=r["ticker"], name=r.get("name"), exchange=r.get("primary_exchange"),
                    security_type=r.get("type"), active=bool(r.get("active", True)),
                    currency=(r.get("currency_name") or "").upper() or None, locale=r.get("locale"),
                    cik=str(r["cik"]).zfill(10) if r.get("cik") else None,
                    composite_figi=r.get("composite_figi"), share_class_figi=r.get("share_class_figi"),
                    last_updated=dtparser.isoparse(lu) if lu else None,
                ))
            on_page(pages)
            url, params = (data or {}).get("next_url"), None
        if url:
            raise ProviderError(PROVIDER, f"ticker list truncated after {max_pages} pages")
        return out

    def list_splits(self, since: date, max_pages: int = 10) -> list[tuple[str, CorporateActionData]]:
        """Market-wide /v3/reference/splits since a date (1000 per page, follows next_url)."""
        url: str | None = "/v3/reference/splits"
        params: dict | None = {"execution_date.gte": since.isoformat(), "limit": 1000, "order": "asc",
                               "sort": "execution_date"}
        out: list[tuple[str, CorporateActionData]] = []
        pages = 0
        while url and pages < max_pages:
            data = self.client.get_json(url, params, cache_ttl=timedelta(hours=12))
            pages += 1
            for raw in (data or {}).get("results") or []:
                try:
                    s = _Split.model_validate(raw)
                except ValidationError:
                    continue
                if raw.get("ticker") and s.split_from > 0 and s.split_to > 0:
                    out.append((raw["ticker"], CorporateActionData(action_type="split", ex_date=s.execution_date,
                                                                   split_from=s.split_from, split_to=s.split_to)))
            url, params = (data or {}).get("next_url"), None
        if url:
            raise ProviderError(PROVIDER, f"split list truncated after {max_pages} pages")
        return out

    def get_grouped_daily(self, day: date, adjusted: bool = False) -> dict[str, PriceBar]:
        data = self.client.get_json(
            f"/v2/aggs/grouped/locale/us/market/stocks/{day}", {"adjusted": "true" if adjusted else "false"},
            cache_ttl=timedelta(hours=12),
        )
        resp = self._validate(_AggsResponse, data, "grouped daily")
        return {a.T: _bar(a) for a in resp.results or [] if a.T}

    @staticmethod
    def _validate(model: type[BaseModel], data: object, what: str):
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise ProviderError(PROVIDER, f"unexpected {what} response shape: {exc.errors()[:2]}") from exc


_limiters: dict[int, RateLimiter] = {}


def _shared_limiter(per_minute: int) -> RateLimiter:
    if per_minute not in _limiters:
        _limiters[per_minute] = RateLimiter(per_minute, 60.0)
    return _limiters[per_minute]
