"""Automated stock discovery without selection bias.

  1. Baseline   – prices, liquidity and SEC financials for EVERY universe stock (baseline.py)
  2. Coverage   – market-wide + rotating company news and SEC filings for the whole universe
                  (universe_news.py); missing coverage is "unknown", never "no catalyst"
  3. Selection  – 60 candidates: 35 baseline leaders, 15 new catalysts, 10 underexplored,
                  deduplicated and refilled; seeded tie-breaks, never alphabetical (selection.py)
  4. Screening  – full quantitative analysis (no AI) + liquidity / data-validity / valuation checks
  5. Scoring    – explicit six-factor 0–100 score (discovery_scoring.py)
  6. AI         – only the top `discovery_ai_top_n` (default 10), then re-scored
  7. Audit      – a DiscoveryCandidate row per universe stock: pathway(s), data timestamps,
                  factors, score and exclusion reason
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import DataMode, get_settings
from app.models import (
    Analysis,
    Company,
    DiscoveryCandidate,
    DiscoveryRun,
    Event,
    EventCompanyRelationship,
    Forecast,
    Recommendation,
)
from app.services.analysis import AnalysisError, analyze_ticker
from app.services.baseline import get_baseline, refresh_baseline
from app.services.discovery_scoring import (
    FACTOR_WEIGHTS,
    FORMULA,
    CatalystStatus,
    ScoreResult,
    clamp01,
    compute_score,
    factor_values,
    financial_strength,  # noqa: F401  (re-exported for callers/tests)
)
from app.services.http import ProviderError
from app.services.market_calendar import NY
from app.services.selection import (
    UniverseItem,
    catalyst_status,
    select_candidates,
)
from app.services.universe import ensure_universe_companies, universe_tickers, watchlist_tickers
from app.services.universe_news import ingest_universe_filings, ingest_universe_news

log = logging.getLogger(__name__)

RANKING_WEIGHTS = FACTOR_WEIGHTS  # backwards-compatible name used by the settings API
NEW_CATALYST_FALLBACK_WINDOW = timedelta(hours=72)


def catalyst_score(events: list[tuple[Event, EventCompanyRelationship]]) -> tuple[float, Event | None]:
    best, best_ev = 0.0, None
    for ev, link in events:
        m = (ev.materiality or 0.3) * link.confidence
        if ev.impact == "negative":
            m *= 0.5
        if ev.status == "unconfirmed":
            m *= 0.5
        if m > best:
            best, best_ev = m, ev
    return clamp01(best), best_ev


def company_events(session: Session, company_id: int) -> list[tuple[Event, EventCompanyRelationship]]:
    now = datetime.now(UTC)
    rows = session.execute(
        select(Event, EventCompanyRelationship).join(EventCompanyRelationship).where(
            EventCompanyRelationship.company_id == company_id,
            ((Event.last_published_at >= now - timedelta(days=14)) & (Event.event_date.is_(None)))
            | ((Event.event_date >= date.today()) & (Event.event_date <= date.today() + timedelta(days=60))),
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


def score_candidate(snapshot: dict[str, Any], cat: float | None, filing_age_days: int | None,
                    status: CatalystStatus = "detected") -> dict[str, Any]:
    """Score one screened candidate (see discovery_scoring for the exact formula)."""
    return compute_score(factor_values(snapshot, status, cat, filing_age_days)).to_dict()


def _last_analyzed(session: Session, mode: str) -> dict[int, datetime]:
    return dict(session.execute(select(Analysis.company_id, func.max(Analysis.created_at))
                                .where(Analysis.data_mode == mode).group_by(Analysis.company_id)).all())


def build_universe_items(session: Session, mode: DataMode, companies: dict[str, Company],
                         now: datetime) -> list[UniverseItem]:
    last = _last_analyzed(session, mode)
    items = []
    for t, c in companies.items():
        b = get_baseline(session, c)
        evs = company_events(session, c.id)
        cat, best_ev = catalyst_score(evs)
        status = catalyst_status(bool(evs), b.news_checked_at, b.filings_checked_at, now)
        m = b.metrics or {}
        items.append(UniverseItem(
            ticker=t, company_id=c.id, eligible=b.eligible, ineligible_reason=b.ineligible_reason,
            baseline_score=m.get("baseline_score"), freshness=m.get("data_freshness"),
            catalyst_status=status, catalyst_score=cat if status == "detected" else None,
            catalyst_first_seen=max((e.created_at for e, _ in evs), default=None),
            last_analyzed_at=last.get(c.id),
            data_timestamps={
                "price_date": b.price_date.isoformat() if b.price_date else None,
                "financials_as_of": b.financials_as_of.isoformat() if b.financials_as_of else None,
                "financials_fetched_at": b.financials_fetched_at.isoformat() if b.financials_fetched_at else None,
                "news_checked_at": b.news_checked_at.isoformat() if b.news_checked_at else None,
                "news_window_start": b.news_window_start.isoformat() if b.news_window_start else None,
                "filings_checked_at": b.filings_checked_at.isoformat() if b.filings_checked_at else None,
                "baseline_updated_at": b.updated_at.isoformat() if b.updated_at else None,
            },
        ))
    return items


def run_discovery(session: Session, mode: DataMode, progress=lambda m: None) -> DiscoveryRun:
    s = get_settings()
    quotas = {"baseline_leader": s.discovery_quota_baseline, "new_catalyst": s.discovery_quota_catalyst,
              "underexplored": s.discovery_quota_underexplored}
    now = datetime.now(UTC)
    seed = now.astimezone(NY).date().isoformat()  # one rotation per trading day, reproducible
    prev = session.scalar(select(DiscoveryRun).where(DiscoveryRun.data_mode == mode, DiscoveryRun.status == "succeeded")
                          .order_by(DiscoveryRun.started_at.desc()))
    new_since = prev.started_at if prev else now - NEW_CATALYST_FALLBACK_WINDOW
    run = DiscoveryRun(data_mode=mode, seed=seed, config={
        "formula": FORMULA, "quotas": quotas, "total_candidates": s.discovery_max_candidates,
        "top_n": s.discovery_top_n, "ai_top_n": s.discovery_ai_top_n,
        "min_avg_dollar_volume": s.min_avg_dollar_volume, "new_catalyst_since": new_since.isoformat(),
    })
    session.add(run)
    session.commit()
    notes: dict[str, Any] = {"errors": []}

    tickers = list(dict.fromkeys(universe_tickers(mode) + watchlist_tickers(session, mode)))
    companies = ensure_universe_companies(session, mode, tickers)
    run.universe_size = len(companies)
    session.commit()

    # 1–2. Baseline + coverage for the whole universe (independent of past analyses)
    notes["baseline"] = refresh_baseline(session, mode, companies, progress)
    try:
        notes["news"] = ingest_universe_news(session, mode, companies, progress=progress)
        notes["filings"] = ingest_universe_filings(session, mode, companies, progress=progress)
    except ProviderError as exc:
        notes["errors"].append(f"coverage: {exc.message}")
    session.commit()

    # 3. Selection
    progress("Selecting candidates")
    items = build_universe_items(session, mode, companies, datetime.now(UTC))
    sel = select_candidates(items, seed, new_since, total=s.discovery_max_candidates, quotas=quotas)
    order = {t: n for n, t in enumerate(sel.tickers, 1)}
    rows: dict[str, DiscoveryCandidate] = {}
    for it in items:
        selected = it.ticker in order
        rows[it.ticker] = DiscoveryCandidate(
            run_id=run.id, company_id=it.company_id, ticker=it.ticker,
            status="selected" if selected else ("ineligible" if not it.eligible else "not_selected"),
            selected=selected, selection_order=order.get(it.ticker), pathway=sel.pathway_of(it.ticker),
            qualifying_pathways=sel.qualifying.get(it.ticker, []), baseline_score=it.baseline_score,
            catalyst_status=it.catalyst_status, catalyst_score=it.catalyst_score,
            previously_analyzed=it.last_analyzed_at is not None, last_analyzed_at=it.last_analyzed_at,
            tie_break_key=sel.tie_keys.get(it.ticker), data_timestamps=it.data_timestamps,
            exclusion_reason=it.ineligible_reason,
        )
        session.add(rows[it.ticker])
    session.commit()
    by_ticker = {i.ticker: i for i in items}

    # 4–5. Screening + scoring
    evaluated: list[dict[str, Any]] = []

    def evaluate(t: str, a: Analysis) -> dict[str, Any] | None:
        row, item = rows[t], by_ticker[t]
        snap = a.snapshot or {}
        row.analysis_id = a.id
        adv = (snap.get("price_stats") or {}).get("avg_dollar_volume_20d")
        reason = None
        if adv is not None and adv < s.min_avg_dollar_volume:
            reason = f"illiquid (20d avg dollar volume {adv:,.0f})"
        elif snap.get("base_target_12m") is None:
            reason = "no valid valuation (missing/implausible data or no applicable method)"
        if reason:
            row.status, row.exclusion_reason = "excluded", reason
            return None
        evs = company_events(session, a.company_id)
        cat, cat_ev = catalyst_score(evs)
        lf = snap.get("latest_filing_date")
        age = (date.today() - date.fromisoformat(lf)).days if lf else None
        result: ScoreResult = compute_score(factor_values(snap, item.catalyst_status, cat, age))
        row.status, row.score, row.factors = "screened", result.score, result.to_dict()
        soon = any(e.event_date and e.event_date <= date.today() + timedelta(days=60) and (e.materiality or 0) >= 0.5
                   for e, _ in evs)
        recent_material = cat_ev is not None and cat_ev.event_date is None and cat >= 0.5
        return {"ticker": t, "analysis": a, "score": result, "catalyst": cat_ev, "age": age, "cat": cat,
                "horizon": "short_term" if (soon or recent_material) else "long_term"}

    for n, t in enumerate(sel.tickers, 1):
        progress(f"Screening {t} ({n}/{len(sel.tickers)})")
        try:
            out = analyze_ticker(session, t, mode, use_ai=False)
            session.commit()
            e = evaluate(t, session.get(Analysis, out.analysis_id))
            if e:
                evaluated.append(e)
        except (AnalysisError, ProviderError) as exc:
            session.rollback()
            rows[t].status, rows[t].exclusion_reason = "excluded", str(exc)
        except Exception as exc:  # keep the run going; record the failure
            session.rollback()
            log.exception("discovery failed for %s", t)
            rows[t].status, rows[t].exclusion_reason = "excluded", f"error: {type(exc).__name__}: {exc}"
            notes["errors"].append({"ticker": t, "error": f"{type(exc).__name__}: {exc}"})
        session.commit()

    def sort_key(e: dict[str, Any]) -> tuple:
        # exact score, then financial strength, then the seeded rotation key (never the ticker name)
        sc = e["score"]
        return (-(sc.exact if sc.exact is not None else -1), -(sc.factors.get("financial_strength") or 0),
                rows[e["ticker"]].tie_break_key or "")

    evaluated.sort(key=sort_key)

    # 6. AI only for the top N, then re-score with the AI-adjusted forecast
    if mode == "live" and s.openai_configured:
        for e in evaluated[: s.discovery_ai_top_n]:
            progress(f"AI analysis {e['ticker']}")
            try:
                out = analyze_ticker(session, e["ticker"], mode, use_ai=True)
                session.commit()
                a = session.get(Analysis, out.analysis_id)
                e2 = evaluate(e["ticker"], a)
                if e2:
                    e.update(e2)
                session.commit()
            except Exception as exc:
                session.rollback()
                notes["errors"].append({"ticker": e["ticker"], "error": f"AI pass: {exc}"})
        evaluated.sort(key=sort_key)

    # 7. Persist ranking + audit
    for rank, e in enumerate(evaluated, 1):
        rows[e["ticker"]].final_rank = rank
        if rank <= s.discovery_top_n:
            rows[e["ticker"]].status = "ranked"
    for rank, e in enumerate(evaluated[: s.discovery_top_n], 1):
        a: Analysis = e["analysis"]
        f12 = session.scalar(select(Forecast).where(Forecast.analysis_id == a.id, Forecast.horizon_months == 12))
        ev = e["catalyst"]
        sc: ScoreResult = e["score"]
        session.add(Recommendation(
            run_id=run.id, company_id=a.company_id, analysis_id=a.id, forecast_id=f12.id if f12 else None, rank=rank,
            score=sc.score, score_band=sc.band, factors=sc.to_dict(),
            outlook=(a.snapshot or {}).get("outlook", "neutral"), risk_rating=(a.snapshot or {}).get("risk_rating", "Medium"),
            horizon_class=e["horizon"], catalyst_event_id=ev.id if ev else None,
            catalyst_title=ev.title if ev else None, catalyst_date=ev.event_date if ev else None,
            selection_pathway=rows[e["ticker"]].pathway,
        ))

    run.stats = run_stats(list(rows.values()), sel, companies)
    run.candidates_evaluated = len(sel.tickers)
    run.status = "succeeded"
    run.finished_at = datetime.now(UTC)
    run.notes = notes
    session.flush()
    return run


def run_stats(rows: list[DiscoveryCandidate], sel, companies: dict[str, Company]) -> dict[str, Any]:
    sel_rows = [r for r in rows if r.selected]
    ranked = sorted([r for r in rows if r.final_rank and r.status == "ranked"], key=lambda r: r.final_rank)
    sector = {t: (c.sector or "Unknown") for t, c in companies.items()}
    letters = Counter(r.ticker[0] for r in sel_rows)
    return {
        "universe": len(rows),
        "eligible": sum(1 for r in rows if r.status != "ineligible"),
        "selected": len(sel_rows),
        "pathway_slots": dict(sel.filled),
        "coverage": dict(Counter(r.catalyst_status for r in rows)),
        "selected_coverage": dict(Counter(r.catalyst_status for r in sel_rows)),
        "previously_unanalyzed_selected": sum(1 for r in sel_rows if not r.previously_analyzed),
        "previously_unanalyzed_ranked": sum(1 for r in ranked if not r.previously_analyzed),
        "selected_sectors": dict(Counter(sector[r.ticker] for r in sel_rows)),
        "ranked_sectors": dict(Counter(sector[r.ticker] for r in ranked)),
        "selected_first_letter_span": f"{min(letters)}–{max(letters)}" if letters else None,
        "excluded": sum(1 for r in rows if r.status == "excluded"),
        "ranked": len(ranked),
    }
