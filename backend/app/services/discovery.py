"""Automated stock discovery: events → candidates → data → forecasts → explainable ranking.

The score is a transparent weighted sum of bounded factors. It deliberately does NOT rank
by projected upside alone, and it is reported in coarse 5-point steps with a band label to
avoid false precision. Weights live in RANKING_WEIGHTS and are stored with every run.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import DataMode, get_settings
from app.models import (
    Analysis,
    Company,
    DiscoveryRun,
    Event,
    EventCompanyRelationship,
    Forecast,
    Recommendation,
)
from app.services.analysis import AnalysisError, analyze_ticker
from app.services.events import ingest_articles, upsert_upcoming
from app.services.http import ProviderError
from app.services.providers import news_provider
from app.services.universe import ensure_universe_companies, universe_tickers, watchlist_tickers

log = logging.getLogger(__name__)

RANKING_WEIGHTS: dict[str, float] = {
    "financial_strength": 0.25,
    "catalyst_materiality": 0.20,
    "valuation": 0.25,
    "data_freshness": 0.10,
    "forecast_uncertainty": -0.10,
    "downside_risk": -0.10,
}


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def financial_strength(s: dict[str, Any]) -> float | None:
    parts = []
    if s.get("net_margin") is not None:
        parts.append(clamp01(s["net_margin"] / 0.20))
    if s.get("fcf_margin") is not None:
        parts.append(clamp01(s["fcf_margin"] / 0.15))
    if s.get("revenue_growth_yoy") is not None:
        parts.append(clamp01((s["revenue_growth_yoy"] + 0.05) / 0.25))
    nc, ni = s.get("net_cash"), s.get("revenue_ttm")
    if nc is not None and ni:
        parts.append(1.0 if nc >= 0 else clamp01(1 + nc / ni))  # net debt relative to revenue
    return sum(parts) / len(parts) if parts else None


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


def score_candidate(snapshot: dict[str, Any], cat: float, filing_age_days: int | None) -> dict[str, Any]:
    fs = financial_strength(snapshot)
    base = snapshot.get("base_return_12m")
    bear = snapshot.get("bear_return_12m")
    bull = snapshot.get("bull_return_12m")
    stale = snapshot.get("stale_sessions", 0) or 0
    factors: dict[str, float | None] = {
        "financial_strength": fs,
        "catalyst_materiality": cat,
        "valuation": clamp01((base + 0.10) / 0.50) if base is not None else None,
        "data_freshness": (1.0 if stale == 0 else 0.7 if stale <= 2 else 0.3)
        * (1.0 if filing_age_days is not None and filing_age_days <= 120 else 0.7),
        "forecast_uncertainty": clamp01((bull - bear) / 1.5) if bull is not None and bear is not None else 0.5,
        "downside_risk": clamp01(-bear / 0.6) if bear is not None else 0.5,
    }
    total = 0.0
    contrib = {}
    for k, w in RANKING_WEIGHTS.items():
        v = factors[k] if factors[k] is not None else (0.0 if w > 0 else 0.5)
        contrib[k] = round(w * v * 100, 1)
        total += w * v
    # theoretical range: all penalties maxed (-0.2) .. all positive factors maxed (+0.8)
    raw = clamp01((total + 0.2) / 1.0) * 100
    score = round(raw / 5) * 5
    band = "Strong" if score >= 70 else "Moderate" if score >= 50 else "Weak"
    return {"score": score, "band": band, "factors": {k: (round(v, 2) if v is not None else None) for k, v in factors.items()},
            "contributions": contrib, "weights": RANKING_WEIGHTS}


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


def ingest_universe_news(session: Session, mode: DataMode, companies: dict[str, Company]) -> dict:
    prov = news_provider(mode, list(companies))
    stats = ingest_articles(session, mode, prov.fetch_recent_news(), companies)
    upsert_upcoming(session, mode, prov.fetch_upcoming_events([]), companies)
    stats["errors"] = getattr(prov, "errors", [])[:10]
    return stats


def run_discovery(session: Session, mode: DataMode, progress=lambda m: None) -> DiscoveryRun:
    s = get_settings()
    run = DiscoveryRun(data_mode=mode, config={"weights": RANKING_WEIGHTS, "top_n": s.discovery_top_n,
                                               "max_candidates": s.discovery_max_candidates,
                                               "ai_top_n": s.discovery_ai_top_n,
                                               "min_avg_dollar_volume": s.min_avg_dollar_volume})
    session.add(run)
    session.flush()
    notes: dict[str, Any] = {"skipped": [], "errors": []}

    progress("Collecting news and events")
    tickers = list(dict.fromkeys(universe_tickers(mode) + watchlist_tickers(session, mode)))
    companies = ensure_universe_companies(session, mode, tickers)
    run.universe_size = len(companies)
    try:
        notes["news"] = ingest_universe_news(session, mode, companies)
    except ProviderError as exc:
        notes["errors"].append(f"news ingestion: {exc.message}")
    session.commit()

    # Candidates: companies tied to recent/upcoming events first, then watchlist, then the rest.
    watch = set(watchlist_tickers(session, mode))
    ranked: list[tuple[float, str]] = []
    for t, c in companies.items():
        cat, _ = catalyst_score(company_events(session, c.id))
        ranked.append((cat + (0.5 if t in watch else 0.0), t))
    ranked.sort(reverse=True)
    candidates = [t for _, t in ranked[: s.discovery_max_candidates]]

    results: list[tuple[str, int]] = []
    for n, t in enumerate(candidates, 1):
        progress(f"Screening {t} ({n}/{len(candidates)})")
        try:
            out = analyze_ticker(session, t, mode, use_ai=False)
            session.commit()
            results.append((t, out.analysis_id))
        except (AnalysisError, ProviderError) as exc:
            session.rollback()
            notes["skipped"].append({"ticker": t, "reason": str(exc)})
        except Exception as exc:  # keep the run going; record the failure
            session.rollback()
            log.exception("discovery failed for %s", t)
            notes["errors"].append({"ticker": t, "error": f"{type(exc).__name__}: {exc}"})

    def evaluate(t: str, aid: int) -> dict[str, Any] | None:
        a = session.get(Analysis, aid)
        c = companies[t]
        snap = a.snapshot or {}
        adv = (snap.get("price_stats") or {}).get("avg_dollar_volume_20d")
        if adv is not None and adv < s.min_avg_dollar_volume:
            notes["skipped"].append({"ticker": t, "reason": f"illiquid (20d avg dollar volume {adv:,.0f})"})
            return None
        if snap.get("base_target_12m") is None:
            notes["skipped"].append({"ticker": t, "reason": "no valuation method applicable"})
            return None
        evs = company_events(session, c.id)
        cat, cat_ev = catalyst_score(evs)
        lf = snap.get("latest_filing_date")
        age = (date.today() - date.fromisoformat(lf)).days if lf else None
        sc = score_candidate(snap, cat, age)
        upcoming = [e for e, _ in evs if e.event_date]
        soon = any(e.event_date <= date.today() + timedelta(days=60) and (e.materiality or 0) >= 0.5 for e in upcoming)
        recent_material = cat_ev is not None and cat_ev.event_date is None and cat >= 0.5
        return {"ticker": t, "analysis": a, "score": sc, "catalyst": cat_ev,
                "horizon": "short_term" if (soon or recent_material) else "long_term"}

    evaluated = [e for e in (evaluate(t, aid) for t, aid in results) if e]
    evaluated.sort(key=lambda e: e["score"]["score"], reverse=True)

    # Second pass: AI-assisted analysis only for the top few (budget control).
    if mode == "live" and s.openai_configured:
        for e in evaluated[: s.discovery_ai_top_n]:
            progress(f"AI analysis {e['ticker']}")
            try:
                out = analyze_ticker(session, e["ticker"], mode, use_ai=True)
                session.commit()
                e["analysis"] = session.get(Analysis, out.analysis_id)
            except Exception as exc:
                session.rollback()
                notes["errors"].append({"ticker": e["ticker"], "error": f"AI pass: {exc}"})

    for rank, e in enumerate(evaluated[: s.discovery_top_n], 1):
        a: Analysis = e["analysis"]
        f12 = session.scalar(select(Forecast).where(Forecast.analysis_id == a.id, Forecast.horizon_months == 12))
        ev = e["catalyst"]
        session.add(Recommendation(
            run_id=run.id, company_id=a.company_id, analysis_id=a.id, forecast_id=f12.id if f12 else None, rank=rank,
            score=e["score"]["score"], score_band=e["score"]["band"], factors=e["score"],
            outlook=(a.snapshot or {}).get("outlook", "neutral"), risk_rating=(a.snapshot or {}).get("risk_rating", "Medium"),
            horizon_class=e["horizon"], catalyst_event_id=ev.id if ev else None,
            catalyst_title=ev.title if ev else None, catalyst_date=ev.event_date if ev else None,
        ))
    run.candidates_evaluated = len(results)
    run.status = "succeeded"
    run.finished_at = datetime.now(UTC)
    run.notes = notes
    session.flush()
    return run
