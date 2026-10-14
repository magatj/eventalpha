"""End-to-end: API → job queue → worker → providers → forecast engine → PostgreSQL."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.db import session_scope
from app.models import AIUsage, NewsArticle
from app.providers.news.base import RawArticle

pytestmark = pytest.mark.integration


def set_mode(client, mode):
    assert client.put("/api/settings", json={"data_mode": mode}).status_code == 200


def analyze(client, run_jobs, ticker, **body):
    r = client.post(f"/api/stocks/{ticker}/analyze", json=body)
    assert r.status_code in (200, 202), r.text
    data = r.json()
    if data["status"] == "cached":
        return data
    run_jobs()
    job = client.get(f"/api/jobs/{data['job_id']}").json()
    return job


def test_health(client):
    r = client.get("/api/health").json()
    assert r["status"] == "ok" and r["database"] == "ok"


def test_demo_vertical_slice(client, run_jobs):
    set_mode(client, "demo")
    job = analyze(client, run_jobs, "nke")
    assert job["status"] == "succeeded", job
    aid = job["result"]["analysis_id"]

    ov = client.get("/api/stocks/NKE").json()
    assert ov["data_mode"] == "demo" and "(DEMO)" in ov["name"]
    assert ov["latest_price"]["provenance"] == "observed" and ov["latest_price"]["price"] > 0

    prices = client.get("/api/stocks/NKE/prices?range=1y").json()
    assert len(prices["bars"]) > 200 and prices["benchmark"]["ticker"] == "SPY"

    fin = client.get("/api/stocks/NKE/financials").json()
    assert fin["ttm"]["revenue"] > 0 and fin["quarterly"]["revenue"]

    fc = client.get("/api/stocks/NKE/forecast").json()
    assert [f["horizon_months"] for f in fc["forecasts"]] == [3, 6, 12, 24, 36, 60]
    f12 = fc["forecasts"][2]
    assert set(f12["scenarios"]) == {"bear", "base", "bull"}
    assert all(s["provenance"] == "model_forecast" for s in f12["scenarios"].values())
    assert f12["assumptions"]["items"] and f12["target_date"]

    an = client.get("/api/stocks/NKE/analysis").json()
    assert an["id"] == aid and an["ai_kind"] == "demo" and an["sources"][0]["label"] == "S1"
    assert an["snapshot"]["risk_rating"] in ("Low", "Medium", "High")

    hist = client.get("/api/stocks/NKE/forecast-history").json()
    assert len(hist["forecasts"]) == 6


def test_cached_analysis_is_reused_and_force_creates_new(client, run_jobs):
    set_mode(client, "demo")
    analyze(client, run_jobs, "MSFT")
    again = client.post("/api/stocks/MSFT/analyze", json={}).json()
    assert again["status"] == "cached"
    forced = analyze(client, run_jobs, "MSFT", force=True)
    assert forced["status"] == "succeeded"
    an = client.get("/api/stocks/MSFT/analysis").json()
    assert an["previous"] is not None and an["diff"] is not None
    hist = client.get("/api/stocks/MSFT/forecast-history").json()
    assert len(hist["forecasts"]) == 12  # both analyses preserved


def test_forecasts_are_immutable(client, run_jobs, engine):
    set_mode(client, "demo")
    analyze(client, run_jobs, "AAPL")
    with engine.connect() as c:
        fid = c.execute(text("SELECT id FROM forecasts LIMIT 1")).scalar()
    for stmt in (f"UPDATE forecasts SET observed_price = 1 WHERE id = {fid}",
                 f"DELETE FROM forecast_scenarios WHERE forecast_id = {fid}"):
        with pytest.raises(DBAPIError, match="immutable"):
            with engine.begin() as c:
                c.execute(text(stmt))


def test_job_dedupe_and_skip_locked(engine):
    from app.services import jobs

    with session_scope() as s:
        j1, c1 = jobs.enqueue(s, "cache_cleanup", "demo", dedupe_key="slot:x")
        j2, c2 = jobs.enqueue(s, "cache_cleanup", "demo", dedupe_key="slot:x")
        assert c1 and not c2 and j1.id == j2.id
    from app.db import session_factory

    s1, s2 = session_factory()(), session_factory()()
    try:
        a = jobs.claim_next(s1, "w1")
        b = jobs.claim_next(s2, "w2")
        assert a is not None and (b is None or b.id != a.id)  # no double-claim
    finally:
        s1.rollback(), s2.rollback(), s1.close(), s2.close()


def test_watchlist_add_remove(client, run_jobs):
    set_mode(client, "demo")
    r = client.post("/api/watchlist", json={"ticker": "avgo"}).json()
    assert r["added"]
    run_jobs()
    items = client.get("/api/watchlist").json()["items"]
    assert [i["ticker"] for i in items] == ["AVGO"] and items[0]["latest_analysis"]
    assert client.post("/api/watchlist", json={"ticker": "AVGO"}).json()["added"] is False
    assert client.delete("/api/watchlist/AVGO").status_code == 200
    assert client.delete("/api/watchlist/AVGO").status_code == 404
    assert client.post("/api/watchlist", json={"ticker": "bad ticker!"}).status_code == 400


def test_event_ingestion_dedupes_and_skips_unchanged(engine):
    from app.services.analysis import get_or_create_company
    from app.services.events import ingest_articles

    now = datetime.now(UTC)
    arts = [
        RawArticle(url="https://a.example/1?utm_source=x", title="Acme Corp (NASDAQ: ACME) raises full-year guidance",
                   published_at=now, provider="test"),
        RawArticle(url="https://b.example/2", title="Acme Corp raises full-year guidance - Wire",
                   published_at=now + timedelta(hours=1), provider="test"),
        RawArticle(url="https://c.example/3", title="Acme reportedly in talks to buy rival, sources say",
                   published_at=now, provider="test"),
    ]
    with session_scope() as s:
        c = get_or_create_company(s, "ACME", "demo")
        c.name = "Acme Corp"
        st1 = ingest_articles(s, "demo", arts, {"ACME": c})
        st2 = ingest_articles(s, "demo", arts, {"ACME": c})
        assert st1["new_articles"] == 3 and st1["merged"] == 1 and st1["new_events"] == 2
        assert st2["skipped"] == 3 and st2["new_articles"] == 0
        rumor = s.scalar(select(NewsArticle).where(NewsArticle.url == "https://c.example/3"))
        assert rumor.is_rumor


def test_live_mode_never_fabricates_prices(client, run_jobs, monkeypatch, fixture):
    """No MASSIVE_API_KEY: identity + SEC data resolve from (recorded) SEC fixtures, but the
    analysis must fail clearly instead of inventing a price."""
    from app.config import get_settings
    from app.providers.sec.edgar import EdgarClient
    from app.services import analysis, providers

    class StubHttp:
        def get_json(self, url, *a, **k):
            if "company_tickers" in url:
                return fixture("sec_company_tickers_exchange.json")
            if "submissions" in url:
                return fixture("sec_submissions_nke.json")
            if "companyfacts" in url:
                return fixture("sec_companyfacts_nke.json")
            raise AssertionError(url)

    stub = EdgarClient(get_settings(), client=StubHttp())
    monkeypatch.setattr(providers, "edgar_client", lambda: stub)
    monkeypatch.setattr(analysis, "edgar_client", lambda: stub)
    monkeypatch.setattr(get_settings(), "massive_api_key", "")
    set_mode(client, "live")
    try:
        job = analyze(client, run_jobs, "NKE")
        assert job["status"] == "failed"
        assert "No price data available for NKE" in job["error"] and "MASSIVE_API_KEY" in job["error"]
        ov = client.get("/api/stocks/NKE").json()
        assert ov["name"] == "NIKE, Inc." and ov["latest_price"] is None
        fin = client.get("/api/stocks/NKE/financials").json()  # real SEC financials were stored
        assert fin["source"].startswith("SEC") and fin["ttm"]["revenue"] > 40e9
    finally:
        set_mode(client, "demo")


def test_ai_engine_caching_and_budget(engine):
    from app.config import Settings
    from app.services.ai import AIEngine, StockThesis

    class FakeResp:
        def __init__(self, parsed):
            self.output_parsed = parsed
            self.model = "fake-model"
            self.usage = type("U", (), {"input_tokens": 1000, "output_tokens": 500})()

    class FakeClient:
        calls = 0

        class responses:  # noqa: N801
            @staticmethod
            def parse(**kw):
                FakeClient.calls += 1
                return FakeResp(StockThesis(
                    summary="s", factual_events=[{"text": "Revenue grew", "claim_type": "verified_fact", "source_ids": ["S1"]}],
                    bullish_arguments=[], bearish_arguments=[], expected_catalysts=[], business_opportunities=[],
                    competitive_risks=[], valuation_risks=[], principal_risks=[], contradictory_evidence=[],
                    revenue_drivers=[], margin_implications=[], short_term_outlook="", long_term_outlook="",
                    outlook="neutral", assumption_suggestions=[], data_gaps=[]))

    settings = Settings(openai_api_key="test", ai_monthly_budget_usd=5.0, openai_input_price_per_m=1.0,
                        openai_output_price_per_m=2.0)
    srcs = [{"id": "S1", "type": "sec_xbrl", "title": "t"}]
    with session_scope() as s:
        s.execute(text("DELETE FROM ai_usage"))
        eng = AIEngine(settings, s, client=FakeClient())
        out1, meta1 = eng.stock_thesis("ACME", "Acme", {"x": 1}, srcs)
        out2, meta2 = eng.stock_thesis("ACME", "Acme", {"x": 1}, srcs)
        assert FakeClient.calls == 1 and meta2["cache_hit"] and out1 == out2
        assert meta1["cost_usd"] == pytest.approx(1000 / 1e6 * 1 + 500 / 1e6 * 2)
        s.add(AIUsage(model="x", purpose="t", cost_usd=5.0))  # exhaust the budget
        s.flush()
        out3, meta3 = eng.stock_thesis("ACME", "Acme", {"x": 2}, srcs)
        assert out3 is None and "budget" in meta3["skipped"] and FakeClient.calls == 1
        s.execute(text("DELETE FROM ai_usage"))


def test_discovery_backtest_and_events_demo(client, run_jobs):
    set_mode(client, "demo")
    r = client.post("/api/discovery/run").json()
    run_jobs(5)
    job = client.get(f"/api/jobs/{r['job_id']}").json()
    assert job["status"] == "succeeded", job
    d = client.get("/api/discovery").json()
    assert 0 < len(d["items"]) <= 20 and d["items"][0]["rank"] == 1
    assert all(i["forecast_provenance"] == "model_forecast" for i in d["items"])
    by_growth = client.get("/api/discovery?sort=growth").json()["items"]
    growth = [i["potential_return"] for i in by_growth]
    assert growth == sorted(growth, reverse=True)
    ev = client.get("/api/events").json()
    assert ev["events"] and any(e["companies"] for e in ev["events"])
    top = d["items"][0]["ticker"]  # a stock this run actually analyzed (dynamic selection varies)
    rp = client.get(f"/api/backtest/replay/{top}?horizon=6").json()
    assert rp["items"] and rp["summary"]["count"] == len(rp["items"])
    assert "benchmark_return" in rp["items"][0]
    bt = client.get("/api/backtest").json()
    assert "summary" in bt


def test_demo_reset_removes_only_demo(client, run_jobs, engine):
    set_mode(client, "demo")
    analyze(client, run_jobs, "TSLA")
    assert client.post("/api/demo/reset").json()["reset"]
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM companies WHERE data_mode='demo'")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM forecasts WHERE data_mode='demo'")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM companies WHERE data_mode='live'")).scalar() >= 1  # untouched


def test_orphaned_running_job_is_recovered(engine):
    """A worker that dies mid-job (e.g. container replaced) must not leave the job 'running'
    forever, and the job's dedupe key must be released so it can be started again."""
    from datetime import UTC

    from app.models import AnalysisJob, DiscoveryRun
    from app.services import jobs

    with session_scope() as s:
        s.execute(text("DELETE FROM analysis_jobs WHERE dedupe_key = 'discovery-manual:live'"))
        job, _ = jobs.enqueue(s, "discovery", "live", dedupe_key="discovery-manual:live")
        claimed = jobs.claim_next(s, "dead-worker:1")
        assert claimed.id == job.id
        s.add(DiscoveryRun(data_mode="live", status="running"))
        s.flush()
        jobs.heartbeat(s, job.id)
        assert jobs.recover_stale(s) == 0  # fresh heartbeat: still considered alive
        s.get(AnalysisJob, job.id).heartbeat_at = datetime.now(UTC) - timedelta(minutes=10)
        s.flush()
        assert jobs.recover_stale(s) >= 1
        j = s.get(AnalysisJob, job.id)
        assert j.status == "failed" and "no heartbeat" in j.error and j.dedupe_key is None
        assert s.scalar(select(DiscoveryRun.status).where(DiscoveryRun.data_mode == "live")
                        .order_by(DiscoveryRun.id.desc())) == "failed"
        _, created = jobs.enqueue(s, "discovery", "live", dedupe_key="discovery-manual:live")
        assert created  # can be re-run
        s.execute(text("DELETE FROM analysis_jobs WHERE job_type = 'discovery' AND data_mode = 'live'"))


def test_discovery_selection_audit_and_unseen_stocks(client, run_jobs, monkeypatch):
    """End-to-end: baseline for the whole universe, pathway-based selection, per-ticker audit,
    and never-analyzed stocks entering the candidate list."""
    from app.config import get_settings

    s = get_settings()
    for k, v in {"discovery_universe_mode": "fixed", "discovery_max_candidates": 10, "discovery_quota_baseline": 5,
                 "discovery_quota_catalyst": 3,
                 "discovery_quota_underexplored": 2, "discovery_top_n": 5}.items():
        monkeypatch.setattr(s, k, v)
    set_mode(client, "demo")
    r = client.post("/api/discovery/run").json()
    run_jobs(5)
    assert client.get(f"/api/jobs/{r['job_id']}").json()["status"] == "succeeded"

    audit = client.get("/api/discovery/candidates?scope=all").json()
    rows = audit["candidates"]
    stats = audit["stats"]
    assert stats["universe"] == len(rows) >= 22  # every universe stock audited, selected or not
    selected = [c for c in rows if c["selected"]]
    assert len(selected) == 10 and stats["selected"] == 10
    assert {c["pathway"] for c in selected} <= {"baseline_leader", "new_catalyst", "underexplored", "refill"}
    assert sum(1 for c in selected if c["pathway"] == "underexplored") == 2
    assert any(not c["previously_analyzed"] for c in selected)  # unseen stocks get in
    assert all(c["data_timestamps"]["price_date"] for c in selected)
    assert {c["catalyst_status"] for c in rows} <= {"detected", "none_detected", "unknown"}
    for c in rows:
        if c["status"] in ("screened", "ranked"):
            assert c["factors"]["score"] == c["score"] and "contributions" in c["factors"]
        if c["status"] in ("excluded", "ineligible"):
            assert c["exclusion_reason"]
    items = client.get("/api/discovery").json()["items"]
    assert items and all(i["selection_pathway"] for i in items)
