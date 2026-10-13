from datetime import UTC, date, datetime, timedelta

import pytest

from app.config import Settings
from app.providers.news.live import economic_calendar, estimate_next_earnings, filings_to_articles
from app.providers.news.rss import parse_feed
from app.providers.sec.edgar import (
    MATERIAL_FORMS,
    EdgarClient,
    describe_8k_items,
    normalize_ticker,
    parse_submissions,
    parse_ticker_map,
)
from app.services.events import (
    canonicalize_url,
    classify,
    extract_tickers,
    heuristic_impact,
    is_rumor,
    materiality_score,
    same_event,
)
from app.services.http import ProviderError

# ---------------------------------------------------------------- SEC identity / filings


def test_ticker_map_and_class_share_normalization(fixture):
    m = parse_ticker_map(fixture("sec_company_tickers_exchange.json"))
    assert m["NKE"].cik == "0000320187" and m["NKE"].exchange == "NYSE"
    assert normalize_ticker("brk.b") == "BRK-B" and "BRK-B" in m


def test_legacy_ticker_format():
    m = parse_ticker_map({"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}})
    assert m["AAPL"].cik == "0000320193"


def test_incorrect_ticker_mapping_is_rejected(fixture):
    class Stub:
        def get_json(self, *a, **k):
            return fixture("sec_company_tickers_exchange.json")

    c = EdgarClient(Settings(sec_user_agent="x a@b.co"), client=Stub())
    assert c.resolve_ticker("nke").name.upper().startswith("NIKE")
    with pytest.raises(ProviderError, match="not in SEC"):
        c.resolve_ticker("ZZZZ")


def test_user_agent_must_identify_contact():
    with pytest.raises(ProviderError, match="contact email"):
        EdgarClient(Settings(sec_user_agent="python-requests"))


def test_submissions_and_filing_events(fixture):
    subs = parse_submissions(fixture("sec_submissions_nke.json"))
    assert subs.fiscal_year_end == "0531" and subs.sic == "3021"
    assert all(f.url.startswith("https://www.sec.gov/Archives/edgar/data/320187/") for f in subs.filings)
    arts = filings_to_articles(subs, "NKE", date(2000, 1, 1))
    assert arts and all(a.kind == "sec_filing" and a.tickers == ["NKE"] for a in arts)
    assert {a.title.split(" filed ")[1].split(" — ")[0] for a in arts} <= MATERIAL_FORMS


def test_estimated_earnings_date_is_labeled_estimated(fixture):
    subs = parse_submissions(fixture("sec_submissions_nke.json"))
    ev = estimate_next_earnings(subs, date(2026, 10, 12))
    assert ev is not None and ev.status == "estimated" and ev.event_date > date(2026, 10, 12)
    assert "Not confirmed" in ev.detail


def test_8k_item_labels():
    assert describe_8k_items("2.02,9.01") == [
        "Item 2.02: Results of operations and financial condition (earnings)", "Item 9.01: Financial statements and exhibits"]
    assert describe_8k_items(None) == []


def test_economic_calendar_window():
    evs = economic_calendar(date(2026, 10, 12), horizon_days=60)
    assert [e.event_date for e in evs] == [date(2026, 10, 28), date(2026, 12, 9)]
    assert all(e.status == "scheduled" and "federalreserve.gov" in e.url for e in evs)


# ---------------------------------------------------------------- RSS / events


def test_rss_parsing(fixture):
    arts = parse_feed(fixture("rss_sample.xml"), "https://example.com/feed.xml")
    assert len(arts) == 2  # empty title skipped
    assert arts[0].summary == "Revenue rose 20%."  # HTML stripped
    assert arts[0].published_at == datetime(2026, 10, 12, 13, 0, tzinfo=UTC)


def test_url_canonicalization_removes_tracking():
    a = canonicalize_url("https://www.Example.com/news/1/?utm_source=x&id=5#top")
    b = canonicalize_url("https://example.com/news/1?id=5&fbclid=abc")
    assert a == b == "https://example.com/news/1?id=5"


def test_deduplicates_same_story_from_different_outlets():
    t0 = datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert same_event("Nike raises full-year revenue guidance - Reuters",
                      "Nike raises full-year revenue guidance, shares jump", t0, t0 + timedelta(hours=3))
    assert not same_event("Nike raises guidance", "Apple unveils new iPhone", t0, t0)
    assert not same_event("Nike raises full-year guidance", "Nike raises full-year guidance", t0, t0 + timedelta(days=5))


def test_rumors_are_not_treated_as_confirmed():
    assert is_rumor("Acme reportedly in talks to buy Widget Co, people familiar say")
    assert is_rumor("Sources say Acme considering sale")
    assert not is_rumor("Acme completes acquisition of Widget Co")


@pytest.mark.parametrize("title,expected", [
    ("Acme reports third-quarter earnings", "earnings"),
    ("Acme raises full-year outlook", "guidance"),
    ("Acme to acquire Widget for $2B", "m_and_a"),
    ("FDA approval granted for Acme drug", "regulatory"),
    ("Acme unveils new data-center chip", "product_launch"),
    ("FOMC statement: interest rates unchanged", "economic_release"),
])
def test_event_classification(title, expected):
    assert classify(title) == expected


def test_impact_heuristic_directions():
    assert heuristic_impact("Acme beats estimates and raises guidance")[0] == "positive"
    assert heuristic_impact("Acme misses estimates, announces layoffs")[0] == "negative"
    assert heuristic_impact("Acme to hold annual meeting")[0] == "uncertain"


def test_company_association_confidence():
    names = {"NKE": "NIKE, Inc.", "AAPL": "Apple Inc.", "T": "AT&T Inc."}
    m = {x.ticker: x for x in extract_tickers("Nike (NYSE: NKE) and Apple partner; $AAPL up", [], names)}
    assert m["NKE"].method == "exchange_ticker" and m["NKE"].confidence == 0.95
    assert m["AAPL"].method == "cashtag"
    # single-letter tickers are not matched from plain words
    assert "T" not in {x.ticker for x in extract_tickers("This is a test", [], names)}
    assert extract_tickers("Unrelated text", ["ZZZ"], names) == []  # tags outside the universe ignored


def test_materiality_penalizes_unconfirmed():
    assert materiality_score("m_and_a", "news", "unconfirmed", 1) < materiality_score("m_and_a", "news", "confirmed", 1)
    assert materiality_score("earnings", "sec_filing", "confirmed", 1) > materiality_score("earnings", "news", "confirmed", 1)
