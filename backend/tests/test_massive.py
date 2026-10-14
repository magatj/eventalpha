from datetime import date

import httpx
import pytest

from app.config import Settings
from app.providers.market.massive import MassiveProvider
from app.services.http import DataUnavailable, HttpClient, ProviderError, ProviderNotConfigured
from app.services.prices import apply_split_adjustment


def provider(routes: dict[str, object]) -> MassiveProvider:
    def handler(req: httpx.Request):
        for prefix, body in routes.items():
            if req.url.path.startswith(prefix):
                if isinstance(body, int):
                    return httpx.Response(body)
                return httpx.Response(200, json=body)
        return httpx.Response(404)

    client = HttpClient("massive", base_url="https://api.massive.test", transport=httpx.MockTransport(handler),
                        sleep=lambda s: None, backoff_base=0.0)
    return MassiveProvider(Settings(massive_api_key=""), client=client)


def test_requires_api_key():
    with pytest.raises(ProviderNotConfigured):
        MassiveProvider(Settings(massive_api_key=""))


def test_latest_price_parses_previous_close(fixture):
    p = provider({"/v2/aggs/ticker/NKE/prev": fixture("massive_prev.json")})
    lp = p.get_latest_price("nke")
    assert lp.price == 71.2 and lp.as_of_date == date(2025, 10, 10) and lp.kind == "previous_close"


def test_latest_price_empty_results_is_unavailable():
    p = provider({"/v2/aggs/ticker/ZZZ/prev": {"status": "OK", "results": []}})
    with pytest.raises(DataUnavailable):
        p.get_latest_price("ZZZ")


def test_historical_prices_and_split_adjustment(fixture):
    p = provider({"/v2/aggs/ticker/NVDA/range": fixture("massive_aggs.json"),
                  "/v3/reference/splits": fixture("massive_splits.json"),
                  "/v3/reference/dividends": {"results": []}})
    bars = p.get_historical_prices("NVDA", date(2024, 6, 1), date(2024, 6, 12))
    assert [b.date for b in bars] == [date(2024, 6, 7), date(2024, 6, 10), date(2024, 6, 11), date(2024, 6, 12)]
    actions = p.get_corporate_actions("NVDA")
    adj = apply_split_adjustment(bars, actions)
    assert adj[0].close == pytest.approx(121.0)  # 1210 pre-split / 10
    assert adj[0].volume == pytest.approx(10000)  # volume scaled up by 10
    assert adj[1].close == 121.8  # on/after ex-date unchanged


def test_company_profile(fixture):
    p = provider({"/v3/reference/tickers/NKE": fixture("massive_ticker_details.json")})
    prof = p.get_company_profile("NKE")
    assert prof.name == "Nike Inc." and prof.security_type == "CS" and prof.currency == "USD"
    assert prof.shares_outstanding == 1477000000


def test_unknown_ticker_profile():
    p = provider({"/v3/reference/tickers/XXXX": 404})
    with pytest.raises(DataUnavailable):
        p.get_company_profile("XXXX")


def test_unexpected_shape_is_a_clear_error():
    p = provider({"/v2/aggs/ticker/NKE/prev": {"results": [{"c": "not-a-number", "t": "x"}]}})
    with pytest.raises(ProviderError, match="unexpected previous close response shape"):
        p.get_latest_price("NKE")


def test_news_parsing_skips_invalid_items(fixture):
    p = provider({"/v2/reference/news": fixture("massive_news.json")})
    news = p.get_company_news("NKE")
    assert len(news) == 1
    assert news[0].source_name == "Example Wire" and news[0].tickers == ["NKE"]
    assert news[0].sentiment == {"NKE": "positive"}


def test_dashboard_shows_actual_index_levels_not_etfs(fixture, monkeypatch):
    from app.providers.market import fred

    monkeypatch.setattr(fred.FredIndexSource, "latest", lambda self, series, lookback_days=21: {
        "SP500": (date(2026, 10, 12), 7807.24, 7811.54), "DJIA": (date(2026, 10, 13), 51670.10, 51850.55)})
    p = provider({"/v2/aggs/ticker/I:NDX": fixture("massive_aggs.json")})
    q = {x.symbol: x for x in p.get_market_indices()}
    assert q["SPX"].kind == "index" and q["SPX"].source == "fred" and q["SPX"].value == 7807.24
    assert q["SPX"].change_pct == pytest.approx(7807.24 / 7811.54 - 1) and q["SPX"].as_of_date == date(2026, 10, 12)
    assert q["DJI"].kind == "index" and q["DJI"].value == 51670.10 and q["DJI"].proxy_for is None
    assert q["NDX"].kind == "index" and q["NDX"].source == "massive"  # entitled on the free plan


def test_etf_is_only_a_labeled_fallback_and_failures_are_not_fabricated(fixture, monkeypatch):
    from app.providers.market import fred

    def fail(self, series, lookback_days=21):
        raise DataUnavailable("fred", "FRED unreachable")

    monkeypatch.setattr(fred.FredIndexSource, "latest", fail)
    p = provider({"/v2/aggs/ticker/SPY": fixture("massive_aggs.json"), "/v2/aggs/ticker/I:NDX": 500,
                  "/v2/aggs/ticker/QQQ": 500, "/v2/aggs/ticker/DIA": 500})
    q = {x.name if x.kind == "index" else x.proxy_for: x for x in p.get_market_indices()}
    spx = q["S&P 500"]
    assert spx.kind == "etf_proxy" and spx.symbol == "SPY" and spx.value == 131.9 and "FRED unreachable" in spx.unavailable_reason
    assert q["Nasdaq-100"].value is None and q["Nasdaq-100"].unavailable_reason  # shown as unavailable


def test_fred_csv_skips_unpublished_sessions():
    from app.providers.market.fred import parse_fred_csv

    d = parse_fred_csv("observation_date,SP500,DJIA\n2026-10-12,7807.24,51850.55\n2026-10-13,,51670.10\n")
    assert d["SP500"] == [(date(2026, 10, 12), 7807.24)] and d["DJIA"][-1] == (date(2026, 10, 13), 51670.10)
