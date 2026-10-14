"""Dynamic market-wide discovery: pure/unit tests with deterministic fixtures (no live APIs)."""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from app.config import Settings
from app.providers.market.base import ListedSecurity
from app.providers.market.massive import MassiveProvider
from app.providers.sec.edgar import parse_daily_index
from app.services import http as http_mod
from app.services.analysis import quant_outlook
from app.services.discovery_scoring import compute_score
from app.services.events import claim_type, extract_tickers, materiality_score, parse_amount
from app.services.http import HttpClient, MemoryCache, ProviderError
from app.services.market_screen import (
    ScreenResult,
    _eligibility,
    _frame_periods,
    fundamental_metrics,
    price_metrics,
    sector_relative_valuation,
)
from app.services.master_universe import security_key, type_status
from app.services.selection import DYNAMIC_QUOTAS, UniverseItem, select_candidates

NOW = datetime(2026, 10, 13, 22, tzinfo=UTC)
EXCH = {"XNYS", "XNAS", "XASE"}


# ------------------------------------------------------------------ 1/4. provider universe + pagination


def _ticker_pages(n_pages: int, per_page: int = 1000):
    def handler(req: httpx.Request):
        page = int(req.url.params.get("cursor", "0"))
        rows = [{"ticker": f"T{page:02d}{i:04d}", "name": f"Co {page}-{i}", "primary_exchange": "XNAS", "type": "CS",
                 "active": True, "currency_name": "usd", "cik": str(100000 + page * per_page + i),
                 "composite_figi": f"BBG{page:03d}{i:06d}", "last_updated_utc": "2026-10-13T06:00:00Z"}
                for i in range(per_page)]
        nxt = f"https://api.massive.test/v3/reference/tickers?cursor={page + 1}" if page + 1 < n_pages else None
        return httpx.Response(200, json={"status": "OK", "results": rows, "next_url": nxt})

    return handler


def provider(handler) -> MassiveProvider:
    client = HttpClient("massive", base_url="https://api.massive.test", transport=httpx.MockTransport(handler),
                        sleep=lambda s: None, backoff_base=0.0)
    return MassiveProvider(Settings(massive_api_key=""), client=client)


def test_ticker_list_follows_pagination_over_large_datasets():
    pages = []
    rows = provider(_ticker_pages(6)).list_securities("CS", on_page=pages.append)
    assert len(rows) == 6000 and pages == [1, 2, 3, 4, 5, 6]
    assert len({r.ticker for r in rows}) == 6000
    assert rows[0].cik == "0000100000" and rows[0].exchange == "XNAS" and rows[0].last_updated is not None


def test_ticker_list_reports_truncation_instead_of_silently_stopping():
    with pytest.raises(ProviderError, match="truncated"):
        provider(_ticker_pages(10)).list_securities("CS", max_pages=3)


def test_rate_limited_calls_are_counted_and_cache_hits_are_not():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"ok": 1})

    c = HttpClient("count-test", base_url="https://x.test", transport=httpx.MockTransport(handler), cache=MemoryCache())
    before = http_mod.call_snapshot()
    for _ in range(3):
        c.get_json("/a", cache_ttl=timedelta(minutes=5))
    d = http_mod.call_delta(before)
    assert calls["n"] == 1 and d["requests"]["count-test"] == 1 and d["cache_hits"]["count-test"] == 2


# ------------------------------------------------------------------ 3. inactive + unsupported excluded


@pytest.mark.parametrize("stype, exch, active, expected", [
    ("CS", "XNYS", True, "unscreened"),
    ("OS", "XNAS", True, "unscreened"),
    ("ETF", "ARCX", True, "excluded_type"),
    ("FUND", "XNAS", True, "excluded_type"),
    ("WARRANT", "XNAS", True, "excluded_type"),
    ("RIGHT", "XNAS", True, "excluded_type"),
    ("PFD", "XNYS", True, "excluded_type"),
    ("CS", "OTC", True, "excluded_type"),          # outside NYSE/Nasdaq/NYSE American
    ("CS", "XNYS", False, "inactive"),
    ("ADRC", "XNYS", True, "needs_alt_fundamentals"),  # discovered, evaluated separately
])
def test_listing_eligibility_by_type_exchange_and_status(stype, exch, active, expected):
    s = ListedSecurity(ticker="X", security_type=stype, exchange=exch, active=active)
    assert type_status(s, EXCH)[0] == expected


def test_stable_identity_prefers_figi():
    assert security_key(ListedSecurity(ticker="A", composite_figi="BBG1", cik="1")) == "FIGI:BBG1"
    assert security_key(ListedSecurity(ticker="A", cik="0000000001")) == "CIK:0000000001:A"
    assert security_key(ListedSecurity(ticker="A")) == "TKR:A"


# ------------------------------------------------------------------ market-wide SEC inputs


def test_daily_index_parsing_handles_company_names_with_spaces():
    txt = """Form Type   Company Name          CIK         Date Filed  File Name
---------------------------------------------------------------------------
8-K              ACME CORP & SONS, INC.                                        1234567     20261009    edgar/data/1234567/0001234567-26-000001.txt
10-Q             BETA HOLDINGS                                                 89          20261009    edgar/data/89/0000000089-26-000002.txt
"""
    rows = parse_daily_index(txt)
    assert [(r.form, r.company, r.cik, r.filed_date) for r in rows] == [
        ("8-K", "ACME CORP & SONS, INC.", "0001234567", date(2026, 10, 9)),
        ("10-Q", "BETA HOLDINGS", "0000000089", date(2026, 10, 9))]
    assert rows[0].url.endswith("0001234567-26-000001.txt")


def test_frame_periods_use_completed_reporting_periods():
    annual, prior, quarters = _frame_periods(date(2026, 10, 13))
    assert (annual, prior) == ("CY2025", "CY2024") and quarters == ["CY2026Q2I", "CY2026Q1I"]
    assert _frame_periods(date(2026, 2, 1))[0] == "CY2024"  # 2025 10-Ks not filed yet


# ------------------------------------------------------------------ 11. no fabrication


def test_missing_financials_are_never_fabricated():
    pm = price_metrics([10.0] * 5, [1e6] * 5, [date(2026, 10, d) for d in range(1, 6)])
    assert pm["sessions"] == 5 and "momentum_3m" not in pm and "volatility" not in pm
    fm = fundamental_metrics({"revenue": {"val": 1e9}}, price=10.0)
    assert fm["net_margin"] is None and fm["market_cap"] is None and fm["free_cash_flow"] is None
    assert fundamental_metrics(None, 10.0) == {}


def test_split_like_jump_disables_momentum_instead_of_guessing():
    closes = [100.0] * 30 + [25.0] * 30
    pm = price_metrics(closes, [1e6] * 60, [date(2026, 1, 1) + timedelta(days=i) for i in range(60)])
    assert "momentum_1m" not in pm and pm["data_flags"]


def test_market_cap_requires_reported_shares():
    fm = fundamental_metrics({"revenue": {"val": 5e9}, "net_income": {"val": 5e8},
                              "shares_outstanding": {"val": 1e8}}, price=50.0)
    assert fm["market_cap"] == pytest.approx(5e9) and fm["earnings_yield"] == pytest.approx(0.1)


# ------------------------------------------------------------------ eligibility thresholds


def _m(**kw):
    m = {"sessions": 60, "stale_sessions": 0, "revenue": 1e9, "net_income": 1e8, "market_cap": 2e9,
         "avg_dollar_volume_20d": 50e6}
    m.update(kw)
    return m


class _Sec:
    def __init__(self, stype="CS"):
        self.security_type = stype


@pytest.mark.parametrize("metrics, cat, expected", [
    (_m(), 0.0, "eligible"),
    (_m(sessions=12), 0.0, "ineligible"),                         # <20 sessions
    (_m(stale_sessions=7), 0.0, "ineligible"),                    # stale
    (_m(market_cap=300e6), 0.0, "ineligible"),                    # small cap
    (_m(avg_dollar_volume_20d=5e6), 0.0, "ineligible"),           # illiquid
    (_m(market_cap=300e6, avg_dollar_volume_20d=8e6), 0.8, "catalyst_eligible"),  # exceptional catalyst
    (_m(market_cap=50e6, avg_dollar_volume_20d=8e6), 0.9, "ineligible"),  # catalyst can't rescue micro-caps
    (_m(market_cap=300e6, avg_dollar_volume_20d=1e6), 0.9, "ineligible"),  # ...nor illiquid ones
    (_m(net_income=None), 0.0, "ineligible"),                     # insufficient financials
])
def test_screening_thresholds_and_catalyst_exception(metrics, cat, expected):
    assert _eligibility(_Sec(), metrics, "sec_full", cat, Settings())[0] == expected


def test_foreign_adr_is_marked_for_alternative_fundamentals_not_dropped():
    status, reason = _eligibility(_Sec("ADRC"), _m(), "foreign", 0.0, Settings())
    assert status == "needs_alt_fundamentals" and "alternative" in reason


def test_valuation_is_sector_aware():
    def r(i, sector_metrics):
        return ScreenResult(i, f"T{i}", "eligible", None, sector_metrics)
    banks = [r(i, {"net_income": 1, "earnings_yield": 0.05 + i / 100, "fcf_yield": 0.5}) for i in range(10)]
    reits = [r(100 + i, {"net_income": 1, "earnings_yield": 0.2}) for i in range(10)]
    sector_relative_valuation(banks + reits, {**{b.security_id: "Financials" for b in banks},
                                              **{x.security_id: "Real Estate" for x in reits}})
    assert banks[-1].valuation_score > banks[0].valuation_score  # higher earnings yield = cheaper
    assert all(x.valuation_score is None and "FFO" in x.metrics["valuation_note"] for x in reits)


# ------------------------------------------------------------------ 6/7/8/9. dynamic pathways


def _items(n=300, seed=1):
    import random

    rng = random.Random(seed)
    out = []
    for i in range(n):
        out.append(UniverseItem(
            ticker=f"S{i:03d}", eligible=i % 7 != 0, security_id=i, financial_score=rng.random(),
            valuation_score=rng.random() if i % 5 else None, completeness=rng.choice([0.4, 0.8, 1.0]),
            catalyst_status="detected" if i % 6 == 0 else "unknown",
            catalyst_score=rng.random() if i % 6 == 0 else None,
            catalyst_first_seen=NOW - timedelta(hours=2) if i % 6 == 0 else None,
            last_analyzed_at=None if i % 3 else NOW - timedelta(days=rng.randint(0, 60)),
            ever_candidate=i % 4 == 0))
    return out


def test_all_four_dynamic_pathways_fill_their_quotas():
    sel = select_candidates(_items(), "2026-10-13", NOW - timedelta(days=1), 60, DYNAMIC_QUOTAS,
                            underexplored_min_completeness=0.6, underexplored_stale_before=NOW - timedelta(days=30))
    assert sel.filled == {"financial_leader": 25, "valuation_opportunity": 15, "new_catalyst": 15,
                          "underexplored": 5, "refill": 0}
    assert len(sel.tickers) == len(set(sel.tickers)) == 60  # a stock never occupies two slots
    multi = {t: p for t, p in sel.qualifying.items() if len(p) > 1}
    assert multi and all(t in sel.tickers for t in multi)  # all qualifying reasons preserved


def test_unused_pathway_slots_are_reallocated():
    items = [i for i in _items() if i.catalyst_status != "detected"]  # no catalysts at all
    sel = select_candidates(items, "s", NOW - timedelta(days=1), 60, DYNAMIC_QUOTAS)
    assert sel.filled["new_catalyst"] == 0 and sel.filled["refill"] == 15 and len(sel.tickers) == 60


def test_dynamic_selection_is_reproducible():
    a = select_candidates(_items(), "seed-x", NOW - timedelta(days=1), 60, DYNAMIC_QUOTAS).selected
    b = select_candidates(list(reversed(_items())), "seed-x", NOW - timedelta(days=1), 60, DYNAMIC_QUOTAS).selected
    assert a == b


def test_new_catalyst_pathway_prefers_stocks_never_in_the_pool():
    items = [UniverseItem(ticker=f"C{i}", eligible=True, financial_score=0.5, catalyst_status="detected",
                          catalyst_score=0.8, catalyst_first_seen=NOW, ever_candidate=i < 5) for i in range(10)]
    sel = select_candidates(items, "s", NOW - timedelta(days=1), 3, {"new_catalyst": 3})
    assert all(not next(i for i in items if i.ticker == t).ever_candidate for t in sel.tickers)


def test_underexplored_requires_adequate_data_and_prefers_newly_eligible():
    items = [UniverseItem(ticker="THIN", eligible=True, completeness=0.3),
             UniverseItem(ticker="OLD", eligible=True, completeness=0.9, last_analyzed_at=NOW - timedelta(days=90)),
             UniverseItem(ticker="RECENT", eligible=True, completeness=0.9, last_analyzed_at=NOW - timedelta(days=2)),
             UniverseItem(ticker="NEWELIG", eligible=True, completeness=0.9, newly_eligible=True),
             UniverseItem(ticker="NEVER", eligible=True, completeness=0.9)]
    sel = select_candidates(items, "s", NOW, 5, {"underexplored": 5}, underexplored_min_completeness=0.6,
                            underexplored_stale_before=NOW - timedelta(days=30))
    assert [t for t, p in sel.selected if p == "underexplored"] == ["NEWELIG", "NEVER", "OLD"]


# ------------------------------------------------------------------ 10. strength != buy


def test_financial_strength_alone_does_not_produce_a_buy():
    strong_but_expensive = {"financial_strength": 1.0, "catalyst_materiality": 0.0, "valuation": 0.0,
                            "data_freshness": 1.0, "forecast_uncertainty": 0.6, "downside_risk": 0.8}
    r = compute_score(strong_but_expensive)
    assert r.score < 50 and r.band == "Weak"
    assert quant_outlook(base=-0.15, bear=-0.5) == "bearish"  # outlook comes from valuation, not quality


# ------------------------------------------------------------------ event detection


@pytest.mark.parametrize("title, kind, rumor, expected", [
    ("Acme files 8-K", "sec_filing", False, "confirmed"),
    ("Acme reportedly in talks to buy Beta", "news", True, "speculation"),
    ("Analyst upgrades Acme to outperform", "news", False, "analyst_interpretation"),
    ("Acme CEO says it expects record demand", "news", False, "management_statement"),
    ("Acme wins $2 billion Army contract", "news", False, "reported"),
])
def test_claim_types_separate_facts_statements_views_and_speculation(title, kind, rumor, expected):
    assert claim_type(title, None, kind, rumor) == expected


def test_article_volume_cannot_make_an_event_material():
    few = materiality_score("industry", "news", "confirmed", 4)
    many = materiality_score("industry", "news", "confirmed", 500)
    assert many == few  # corroboration bonus is capped
    assert materiality_score("guidance", "news", "confirmed", 1) > many


def test_amount_parsing():
    assert parse_amount("wins $2.5 billion contract and $300 million option") == 2.5e9
    assert parse_amount("no dollars here") is None


def test_news_can_reference_stocks_outside_the_monitored_set():
    names = {"AAPL": "Apple Inc."}
    m = extract_tickers("Tiny (NASDAQ: TINY) wins contract", ["TINY"], names, known_tickers={"TINY", "AAPL"})
    assert [(x.ticker, x.method) for x in m] == [("TINY", "exchange_ticker")]
    # unknown symbols that aren't listed securities are ignored
    assert extract_tickers("$ZZZZ rallies", [], names, known_tickers={"TINY"}) == []
