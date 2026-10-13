from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.scheduler.main import enqueue_slot, slot_key
from app.services.ai import rule_based_thesis, validate_citations
from app.services.analysis import AnalysisError, normalize_ticker, quant_outlook, risk_rating
from app.services.discovery import RANKING_WEIGHTS, score_candidate
from app.services.market_calendar import (
    is_early_close,
    is_trading_day,
    last_completed_session,
    previous_session,
    trading_days_stale,
)

NY = ZoneInfo("America/New_York")

# ---------------------------------------------------------------- exchange calendar


def test_non_trading_days():
    assert not is_trading_day(date(2025, 12, 25))  # Christmas
    assert not is_trading_day(date(2025, 7, 4))  # Independence Day
    assert not is_trading_day(date(2025, 10, 11))  # Saturday
    assert is_trading_day(date(2025, 10, 10))


def test_early_close():
    assert is_early_close(date(2025, 11, 28))  # day after Thanksgiving, 1pm close
    assert not is_early_close(date(2025, 11, 26))


def test_last_completed_session_respects_close_time():
    before_close = datetime(2025, 10, 10, 15, 0, tzinfo=NY)
    after_close = datetime(2025, 10, 10, 17, 0, tzinfo=NY)
    weekend = datetime(2025, 10, 12, 12, 0, tzinfo=NY)
    assert last_completed_session(before_close) == date(2025, 10, 9)
    assert last_completed_session(after_close) == date(2025, 10, 10)
    assert last_completed_session(weekend) == date(2025, 10, 10)
    # early close: complete by 14:00 ET on the day after Thanksgiving
    assert last_completed_session(datetime(2025, 11, 28, 14, 0, tzinfo=NY)) == date(2025, 11, 28)


def test_stale_price_detection():
    now = datetime(2025, 10, 14, 18, 0, tzinfo=NY)  # Tuesday evening
    assert trading_days_stale(date(2025, 10, 14), now) == 0
    assert trading_days_stale(date(2025, 10, 10), now) == 2  # missed Mon + Tue
    assert previous_session(date(2025, 12, 26)) == date(2025, 12, 24)


# ---------------------------------------------------------------- ticker validation


@pytest.mark.parametrize("raw,expected", [("nke", "NKE"), (" brk.b ", "BRK.B"), ("BF-B", "BF-B")])
def test_valid_tickers(raw, expected):
    assert normalize_ticker(raw) == expected


@pytest.mark.parametrize("raw", ["", "TOOLONGX", "N1KE", "AAPL;DROP", "$NVDA"])
def test_invalid_tickers(raw):
    with pytest.raises(AnalysisError) as e:
        normalize_ticker(raw)
    assert e.value.code == "invalid_ticker"


# ---------------------------------------------------------------- risk/outlook/ranking


def test_risk_rating_levels():
    assert risk_rating(0.2, -0.1, 0.2, 1e9, 1e9, 0)[0] == "Low"
    assert risk_rating(0.5, -0.5, -0.1, -1e10, 1e9, 3)[0] == "High"


def test_quant_outlook():
    assert quant_outlook(0.2, -0.1) == "bullish"
    assert quant_outlook(0.2, -0.5) == "neutral"  # big downside tempers a bullish call
    assert quant_outlook(-0.1, -0.3) == "bearish"
    assert quant_outlook(None, None) == "neutral"


def snap(**kw):
    s = dict(net_margin=0.2, fcf_margin=0.15, revenue_growth_yoy=0.15, net_cash=1e9, revenue_ttm=10e9,
             base_return_12m=0.2, bear_return_12m=-0.15, bull_return_12m=0.5, stale_sessions=0)
    s.update(kw)
    return s


def test_ranking_is_not_upside_alone():
    quality = score_candidate(snap(), cat=0.6, filing_age_days=30)
    junk_with_upside = score_candidate(snap(net_margin=-0.3, fcf_margin=-0.2, revenue_growth_yoy=-0.2, net_cash=-20e9,
                                            base_return_12m=0.6, bear_return_12m=-0.8, bull_return_12m=2.0),
                                       cat=0.6, filing_age_days=30)
    assert quality["score"] > junk_with_upside["score"]


def test_score_is_coarse_and_explained():
    s = score_candidate(snap(), cat=0.5, filing_age_days=30)
    assert s["score"] % 5 == 0 and s["band"] in ("Strong", "Moderate", "Weak")
    assert set(s["contributions"]) == set(RANKING_WEIGHTS)


def test_stale_data_lowers_score():
    fresh = score_candidate(snap(), 0.5, 30)["factors"]["data_freshness"]
    stale = score_candidate(snap(stale_sessions=5), 0.5, 400)["factors"]["data_freshness"]
    assert stale < fresh


# ---------------------------------------------------------------- AI guardrails (no network)


def test_citation_validation_rejects_unsourced_facts():
    thesis = {
        "bullish_arguments": [
            {"text": "Revenue grew 20%", "claim_type": "verified_fact", "source_ids": ["S1"]},
            {"text": "Invented fact", "claim_type": "verified_fact", "source_ids": ["S99"]},
            {"text": "Could expand margins", "claim_type": "speculative", "source_ids": []},
            {"text": "Price target of $500", "claim_type": "model_assumption", "source_ids": ["S1"]},
        ],
        "assumption_suggestions": [
            {"scenario": "base", "revenue_growth_adjustment_pp": 0.02, "net_margin_adjustment_pp": 0, "rationale": "x",
             "source_ids": ["S42"]},
        ],
    }
    out, rejected = validate_citations(thesis, {"S1", "S2"})
    texts = [c["text"] for c in out["bullish_arguments"]]
    assert "Invented fact" not in texts and rejected[0]["text"] == "Invented fact"
    assert "Could expand margins" in texts
    assert "warning" in [c for c in out["bullish_arguments"] if "Price target" in c["text"]][0]
    assert out["assumption_suggestions"] == []  # suggestion without a valid source dropped


def test_rule_based_thesis_cites_data_and_has_no_targets():
    m = {"revenue_growth_yoy": 0.25, "net_margin": 0.3, "fcf_margin": 0.2, "net_cash": 5e9, "pe_ttm": 50,
         "pe_band": [18, 27, 38], "volatility_1y": 0.4, "base_return_12m": -0.1, "bear_return_12m": -0.4}
    srcs = [{"id": "S1", "type": "sec_xbrl"}, {"id": "S2", "type": "market_data"}]
    t = rule_based_thesis(m, srcs, [], demo=False)
    assert t["outlook"] == "bearish"
    assert all(c["source_ids"] == ["S1"] for c in t["factual_events"])
    assert t["valuation_risks"] and "no language model" in t["summary"]


# ---------------------------------------------------------------- scheduler


def test_slot_keys():
    now = datetime(2026, 10, 12, 15, 30, tzinfo=NY)
    assert slot_key("ingest_news", "live", now, "hour") == "ingest_news:live:2026-10-12T15"
    assert slot_key("sec_monitor", "live", now, "2hour") == "sec_monitor:live:2026-10-12T14"
    assert slot_key("forecast_review", "demo", now, "week") == "forecast_review:demo:2026-W42"


def test_trading_day_jobs_skip_holidays_without_touching_db():
    christmas = lambda: datetime(2025, 12, 25, 18, 0, tzinfo=NY)  # noqa: E731
    assert enqueue_slot("discovery", "day", trading_days_only=True, now_fn=christmas) is None
