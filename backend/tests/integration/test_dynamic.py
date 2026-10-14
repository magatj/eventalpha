"""Dynamic market-wide discovery against real PostgreSQL (demo provider = deterministic fixture market)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from app.db import session_scope
from app.models import (
    Company,
    DiscoveryCandidate,
    Recommendation,
    Security,
    SecurityTickerHistory,
)
from app.providers.market.base import ListedSecurity
from app.providers.news.base import RawArticle

pytestmark = pytest.mark.integration


def L(t, figi, stype="CS", active=True, exch="XNAS", cik=None):
    return ListedSecurity(ticker=t, name=f"{t} Inc", exchange=exch, security_type=stype, active=active,
                          composite_figi=figi, cik=cik)


def test_master_universe_lifecycle(engine):
    """New listings enter automatically; delistings are kept (inactive); ticker changes keep the
    same identity and the linked company (so historical forecasts stay attached)."""
    from app.services.master_universe import refresh_master_universe

    mode = "live"  # injected listings: no provider calls
    with session_scope() as s:
        s.execute(text("DELETE FROM securities WHERE data_mode='live' AND security_key LIKE 'FIGI:TST%'"))
        s.execute(text("DELETE FROM companies WHERE data_mode='live' AND ticker IN ('OLDT','NEWT','GONE','KEEP','FRESH','ETFX')"))
        first = [L("OLDT", "TST1"), L("GONE", "TST2"), L("KEEP", "TST3"), L("ETFX", "TST4", stype="ETF", exch="ARCX")]
        refresh_master_universe(s, mode, listings=first)
        comp = Company(ticker="OLDT", data_mode=mode, name="OLDT Inc")
        s.add(comp)
        s.flush()
        sec = s.scalar(select(Security).where(Security.security_key == "FIGI:TST1"))
        sec.company_id = comp.id
        s.commit()
        assert s.scalar(select(Security).where(Security.security_key == "FIGI:TST4")).eligibility_status == "excluded_type"

        second = [L("NEWT", "TST1"), L("KEEP", "TST3"), L("FRESH", "TST5"), L("ETFX", "TST4", stype="ETF", exch="ARCX")]
        stats = refresh_master_universe(s, mode, listings=second)
        assert stats["new"] == 1 and stats["ticker_changes"] == 1 and stats["delisted"] == 1

        renamed = s.scalar(select(Security).where(Security.security_key == "FIGI:TST1"))
        assert renamed.id == sec.id and renamed.ticker == "NEWT"
        assert s.get(Company, comp.id).ticker == "NEWT"  # same company row -> forecasts stay attached
        gone = s.scalar(select(Security).where(Security.security_key == "FIGI:TST2"))
        assert not gone.active and gone.eligibility_status == "inactive" and gone.delisted_at  # kept, not deleted
        assert s.scalar(select(Security).where(Security.security_key == "FIGI:TST5")).active  # entered automatically
        kinds = {h.change_type for h in s.scalars(select(SecurityTickerHistory).where(
            SecurityTickerHistory.security_id.in_([sec.id, gone.id])))}
        assert {"listed", "ticker_change", "delisted"} <= kinds
        s.execute(text("DELETE FROM securities WHERE data_mode='live' AND security_key LIKE 'FIGI:TST%'"))
        s.execute(text("DELETE FROM companies WHERE id = :i"), {"i": comp.id})


def test_partial_refresh_never_mass_delists(engine):
    from app.services.master_universe import refresh_master_universe

    with session_scope() as s:
        s.execute(text("DELETE FROM securities WHERE data_mode='live' AND security_key LIKE 'FIGI:PRT%'"))
        refresh_master_universe(s, "live", listings=[L(f"P{i}", f"PRT{i}") for i in range(10)])
        stats = refresh_master_universe(s, "live", listings=[L("P0", "PRT0")])  # e.g. a failed/partial fetch
        assert stats.get("delisted", 0) == 0 and any("skipped" in e for e in stats["errors"])
        s.execute(text("DELETE FROM securities WHERE data_mode='live' AND security_key LIKE 'FIGI:PRT%'"))


def test_news_introduces_a_never_monitored_stock(engine):
    from app.services.events import ingest_articles
    from app.services.master_universe import make_resolver, refresh_master_universe

    with session_scope() as s:
        refresh_master_universe(s, "demo", listings=None)  # demo market listing
        resolver, known = make_resolver(s, "demo")
        from app.demo.synthetic import DEMO_EXTRA

        unseen = next(t for t in DEMO_EXTRA if t in known and not s.scalar(
            select(Company).where(Company.ticker == t, Company.data_mode == "demo")))
        art = RawArticle(url=f"https://news.example/{unseen}", title=f"{unseen} wins $1 billion multi-year contract",
                         tickers=[unseen], provider="test", published_at=datetime.now(UTC))
        st = ingest_articles(s, "demo", [art], {}, resolver, known)
        assert st.get("new_companies") == 1 and st["links"] == 1
        comp = s.scalar(select(Company).where(Company.ticker == unseen, Company.data_mode == "demo"))
        sec = s.scalar(select(Security).where(Security.ticker == unseen, Security.data_mode == "demo"))
        assert comp is not None and sec.company_id == comp.id


def test_dynamic_discovery_end_to_end(engine, client, monkeypatch):
    """Universe from the provider (not universe.txt), four pathways, deep analysis, AI limited to
    the top N, publishing threshold, and a complete audit trail — with no AWS credentials."""
    from app.config import get_settings
    from app.demo.synthetic import DEMO_EXTRA, DEMO_UNIVERSE
    from app.services import dynamic_discovery as dd
    from app.services.universe import UNIVERSE_FILE

    s = get_settings()
    for k, v in {"discovery_universe_mode": "dynamic", "discovery_max_candidates": 12, "dynamic_quota_financial": 4,
                 "dynamic_quota_valuation": 3, "dynamic_quota_catalyst": 3, "dynamic_quota_underexplored": 2,
                 "discovery_top_n": 5, "discovery_ai_top_n": 3, "discovery_min_publish_score": 0.0}.items():
        monkeypatch.setattr(s, k, v)

    ai_calls: list[str] = []
    real = dd.analyze_ticker

    def counting(session, ticker, mode, force=False, progress=lambda m: None, use_ai=True):
        if use_ai:
            ai_calls.append(ticker)
        return real(session, ticker, mode, force=force, progress=progress, use_ai=False)  # no network in tests

    monkeypatch.setattr(dd, "analyze_ticker", counting)
    monkeypatch.setattr(dd, "ai_enabled", lambda mode: True)

    with session_scope() as sess:
        run = dd.run_dynamic_discovery(sess, "demo")
        run_id, stats = run.id, run.stats
    assert run.status == "succeeded" and run.universe_mode == "dynamic" and run.universe_version

    with session_scope() as sess:
        listed = {x.ticker for x in sess.scalars(select(Security).where(Security.data_mode == "demo"))}
        seed_file = {ln.strip() for ln in UNIVERSE_FILE.read_text().splitlines() if ln.strip() and not ln.startswith("#")}
        assert set(DEMO_EXTRA) & listed and not (listed <= seed_file)  # universe came from the provider
        rows = list(sess.scalars(select(DiscoveryCandidate).where(DiscoveryCandidate.run_id == run_id)))
        assert len(rows) == stats["screened"] > len(DEMO_UNIVERSE)
        selected = [r for r in rows if r.selected]
        assert len(selected) == 12 and len({r.ticker for r in selected}) == 12
        assert stats["pathway_slots"] == {"financial_leader": 4, "valuation_opportunity": 3, "new_catalyst": 3,
                                          "underexplored": 2, "refill": 0} or stats["pathway_slots"]["refill"] > 0
        assert any(r.ticker in DEMO_EXTRA for r in selected)  # stocks outside the old fixed list get selected
        for r in rows:
            assert r.eligibility_status and r.screen_metrics is not None and r.data_timestamps
            if r.eligibility_status in ("ineligible", "needs_alt_fundamentals"):
                assert r.exclusion_reason
        # only the top N received deep AI analysis
        assert len(ai_calls) <= 3
        assert sum(1 for r in selected if r.ai_status == "completed" or r.ai_status == "skipped") == len(ai_calls)
        recs = list(sess.scalars(select(Recommendation).where(Recommendation.run_id == run_id)))
        assert 0 < len(recs) <= 5
        for rec in recs:  # complete audit trail for every published recommendation
            row = next(r for r in selected if r.company_id == rec.company_id)
            assert row.status == "ranked" and row.final_rank and row.reasons and row.factors["contributions"]
            assert row.model_versions["forecast_model"] and row.analysis_id == rec.analysis_id
            assert rec.selection_pathway == row.pathway and rec.score == pytest.approx(row.score)
        # quantitative analysis exists independently of OpenAI (demo: no AI at all)
        assert all(r.analysis_id for r in selected if r.status in ("screened", "ranked"))

    body = client.get("/api/discovery/overview").json()
    assert body["master_universe"]["total"] >= stats["screened"]
    items = client.get("/api/discovery").json()["items"]
    assert items and all(i["reasons"] and "score_display" in i for i in items)
    csv = client.get(f"/api/discovery/audit.csv?run_id={run_id}")
    assert csv.status_code == 200 and csv.text.count("\n") == stats["screened"] + 1


def test_publishing_can_return_fewer_than_top_n(engine, monkeypatch):
    from app.config import get_settings
    from app.services import dynamic_discovery as dd

    s = get_settings()
    for k, v in {"discovery_max_candidates": 6, "dynamic_quota_financial": 3, "dynamic_quota_valuation": 1,
                 "dynamic_quota_catalyst": 1, "dynamic_quota_underexplored": 1, "discovery_top_n": 20,
                 "discovery_min_publish_score": 99.0}.items():
        monkeypatch.setattr(s, k, v)
    with session_scope() as sess:
        run = dd.run_dynamic_discovery(sess, "demo")
        assert run.stats["published"] < 20
        below = [r for r in sess.scalars(select(DiscoveryCandidate).where(DiscoveryCandidate.run_id == run.id))
                 if r.status == "screened"]
        assert all("below publish minimum" in (r.exclusion_reason or "") for r in below)


def test_watchlist_stays_supported_in_dynamic_mode(client, run_jobs, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "discovery_universe_mode", "dynamic")
    assert client.put("/api/settings", json={"data_mode": "demo"}).status_code == 200
    assert client.post("/api/watchlist", json={"ticker": "QQQQ"}).json()["added"]  # not in the demo market
    run_jobs()
    items = client.get("/api/watchlist").json()["items"]
    q = next(i for i in items if i["ticker"] == "QQQQ")
    assert q["latest_analysis"] and q["latest_price"]  # monitored + analyzable outside the candidate pool
    assert client.post("/api/stocks/QQQQ/analyze", json={"force": True}).status_code == 202
    run_jobs()
    hist = client.get("/api/stocks/QQQQ/forecast-history?horizon=12").json()["forecasts"]
    assert len(hist) >= 2  # forecasts refresh and history is preserved
    client.delete("/api/watchlist/QQQQ")


def test_estimated_calendar_entries_are_not_new_catalysts(engine):
    """Estimated earnings dates count toward catalyst importance but can't qualify a stock as a
    *newly detected* catalyst (they're only computed for monitored companies — that would bias)."""
    from app.models import Event, EventCompanyRelationship
    from app.services.dynamic_discovery import security_catalysts

    now = datetime.now(UTC)
    with session_scope() as s:
        comp = Company(ticker="ESTX", data_mode="demo", name="Est Co")
        s.add(comp)
        s.flush()
        sec = Security(data_mode="demo", security_key="FIGI:ESTX", ticker="ESTX", security_type="CS",
                       provider="test", company_id=comp.id)
        s.add(sec)

        def link(title, claim, status, event_date=None):
            ev = Event(data_mode="demo", fingerprint=f"{title}-{now.timestamp()}", title=title, event_type="earnings",
                       status=status, claim_type=claim, impact="uncertain", event_date=event_date, materiality=0.6,
                       last_published_at=now, first_published_at=now)
            s.add(ev)
            s.flush()
            s.add(EventCompanyRelationship(event_id=ev.id, company_id=comp.id, confidence=0.9, method="test"))
            s.flush()

        link("ESTX estimated next earnings", "estimated", "estimated", now.date())
        score, _best, first_seen, n, detected = security_catalysts(s, "demo", {}, now)[sec.id]
        assert score > 0 and n == 1  # counts toward catalyst importance
        assert detected == 0.0 and first_seen is None  # ...but is not a newly detected catalyst
        link("ESTX files 8-K: results of operations", "confirmed", "confirmed")
        score, _best, first_seen, n, detected = security_catalysts(s, "demo", {}, now)[sec.id]
        assert detected > 0 and first_seen is not None and n == 2
        s.rollback()
