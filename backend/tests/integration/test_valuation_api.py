"""Valuation audit end-to-end: API compatibility, audit completeness, and ranking exclusion of
unsupported results (with the audit record preserved)."""

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import Analysis, DiscoveryCandidate, Forecast, Recommendation

from .test_platform import analyze, set_mode

pytestmark = pytest.mark.integration

AUDIT_KEYS = {"status", "status_label", "model_version", "method", "method_label", "business_model", "rationale",
              "required_inputs", "missing_inputs", "historical_multiples", "peers", "multiples", "earnings_state",
              "scenarios_12m", "guardrails", "market_cap", "financial_status", "validation", "financial_coverage",
              "provenance", "data_timestamps"}
SCENARIO_KEYS = {"revenue", "revenue_growth", "net_margin", "net_income", "eps", "free_cash_flow", "capex", "shares",
                 "pe_multiple", "target_price", "potential_return", "target_date", "method_weights"}


def test_analysis_stores_and_serves_a_complete_valuation_audit(client, run_jobs):
    set_mode(client, "demo")
    job = analyze(client, run_jobs, "MSFT", force=True)
    assert job["status"] == "succeeded", job
    an = client.get("/api/stocks/MSFT/analysis").json()
    v = an["valuation"]
    assert AUDIT_KEYS <= set(v)
    assert v["status"] in ("valid", "limited_data", "requires_review", "unsupported_valuation", "invalid_inputs")
    assert an["valuation_status"] == v["status"] and an["valuation_method"] == v["method_label"]
    assert set(v["scenarios_12m"]) == {"bear", "base", "bull"}
    for s in v["scenarios_12m"].values():
        assert SCENARIO_KEYS <= set(s)
    assert v["guardrails"]["status"] == v["status"] and isinstance(v["guardrails"]["flags"], list)
    assert v["data_timestamps"]["price_date"] and v["model_version"] == "quant-2.0.0"

    # existing contracts unchanged: forecast endpoint, overview and snapshot fields
    fc = client.get("/api/stocks/MSFT/forecast").json()
    assert [f["horizon_months"] for f in fc["forecasts"]] == [3, 6, 12, 24, 36, 60]
    f12 = fc["forecasts"][2]
    assert f12["valuation_methods"]["plan"]["method"] == v["method"]
    assert f12["valuation_methods"]["forward_pe"]["multiples"]["basis"]
    ov = client.get("/api/stocks/MSFT").json()
    assert ov["latest_analysis"]["valuation_status"] == v["status"] and ov["market_cap"]["basis"]
    assert an["snapshot"]["valuation_status"] == v["status"]
    if v["status"] in ("valid", "limited_data", "requires_review"):  # targets shown (review: flagged, not ranked)
        assert v["scenarios_12m"]["base"]["target_price"] == pytest.approx(f12["scenarios"]["base"]["target_price"])


def test_unsupported_results_are_not_ranked_but_their_audit_is_kept(engine, monkeypatch):
    from app.config import get_settings
    from app.services import analysis as analysis_mod
    from app.services import dynamic_discovery as dd
    from app.services.forecasting.valuation_guardrails import REVIEW, Flag, GuardrailResult

    s = get_settings()
    for k, v in {"discovery_universe_mode": "dynamic", "discovery_max_candidates": 6, "dynamic_quota_financial": 2,
                 "dynamic_quota_valuation": 2, "dynamic_quota_catalyst": 1, "dynamic_quota_underexplored": 1,
                 "discovery_top_n": 5, "discovery_min_publish_score": 0.0,
                 "analysis_cache_ttl_hours": 0}.items():  # fresh analyses (no reuse of earlier tests' results)
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(analysis_mod, "assess", lambda *a, **kw: GuardrailResult(
        REVIEW, [Flag("unsupported_upside", "review", "Base-case upside exceeds 100% without support.")]))

    with session_scope() as sess:
        run = dd.run_dynamic_discovery(sess, "demo")
        run_id, stats = run.id, run.stats
    with session_scope() as sess:
        rows = [r for r in sess.scalars(select(DiscoveryCandidate).where(DiscoveryCandidate.run_id == run_id)) if r.selected]
        analyzed = [r for r in rows if r.analysis_id]
        assert analyzed
        for r in analyzed:
            assert r.status == "excluded" and r.exclusion_reason.startswith("valuation requires review")
            a = sess.get(Analysis, r.analysis_id)
            assert a.valuation_status == "requires_review" and a.valuation["guardrails"]["flags"]  # audit preserved
            assert sess.scalars(select(Forecast).where(Forecast.analysis_id == a.id)).first() is not None
        assert not list(sess.scalars(select(Recommendation).where(Recommendation.run_id == run_id)))
    assert stats["valuation_statuses"].get("requires_review", 0) == len(analyzed)
    cov = stats["coverage_metrics"]
    for k in ("securities_discovered", "valid_market_cap", "with_revenue", "with_net_income", "with_cash_flow",
              "valid_valuation_method", "eligible", "requiring_alternative_provider"):
        assert isinstance(cov[k], int)
    assert cov["valid_market_cap"] <= cov["screened"] and cov["eligible"] == stats["eligible"]


def test_discovery_outputs_expose_valuation_status_and_exclusion_codes(engine, client, monkeypatch):
    from app.config import get_settings
    from app.services import dynamic_discovery as dd

    set_mode(client, "demo")
    s = get_settings()
    for k, v in {"discovery_universe_mode": "dynamic", "discovery_max_candidates": 8, "dynamic_quota_financial": 3,
                 "dynamic_quota_valuation": 2, "dynamic_quota_catalyst": 2, "dynamic_quota_underexplored": 1,
                 "discovery_top_n": 5, "discovery_min_publish_score": 0.0}.items():
        monkeypatch.setattr(s, k, v)
    with session_scope() as sess:
        run_id = dd.run_dynamic_discovery(sess, "demo").id
    items = client.get("/api/discovery").json()["items"]
    assert all(i["valuation_status"] in ("valid", "limited_data") for i in items)  # only supported results published
    header = client.get(f"/api/discovery/audit.csv?run_id={run_id}").text.splitlines()[0].split(",")
    for col in ("exclusion_codes", "market_cap_basis", "fundamentals_source", "valuation_status", "valuation_method"):
        assert col in header
    cands = client.get(f"/api/discovery/candidates?run_id={run_id}&scope=all&limit=5000").json()["candidates"]
    inel = [c for c in cands if c["eligibility_status"] in ("ineligible", "needs_alt_fundamentals")]
    assert inel and all(c["exclusion_codes"] for c in inel)  # every exclusion has machine-readable codes


def test_market_split_refresh_tolerates_duplicate_provider_rows(engine, monkeypatch):
    from datetime import date, timedelta

    from app.models import MarketSplit
    from app.providers.market.base import CorporateActionData
    from app.services import providers
    from app.services.screen_coverage import refresh_market_splits, splits_by_ticker

    class P:
        def list_splits(self, since):
            a = CorporateActionData(action_type="split", ex_date=date(2026, 6, 12), split_from=1, split_to=10)
            r = CorporateActionData(action_type="split", ex_date=date(2025, 7, 21), split_from=200, split_to=1)
            return [("KLAC", a), ("KLAC", a), ("GVH", r)]

    monkeypatch.setattr(providers, "market_provider", lambda mode: P())
    with session_scope() as sess:
        assert refresh_market_splits(sess, "demo", max_age=timedelta(0))["splits"] == 2
        m = splits_by_ticker(sess, "demo")
        assert m["KLAC"] == [(date(2026, 6, 12), 10.0)] and m["GVH"][0][1] == pytest.approx(0.005)  # reverse split
        sess.query(MarketSplit).filter(MarketSplit.data_mode == "demo").delete()
