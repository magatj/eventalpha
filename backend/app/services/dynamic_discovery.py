"""Dynamic, market-wide discovery (DISCOVERY_UNIVERSE_MODE=dynamic).

Progressive funnel — expensive work only happens at the narrow end:

  1. Master universe      every US-listed security from the provider (weekly refresh)
  2. Market data          grouped-daily prices + SEC frames fundamentals (incremental, daily)
  3. Events               market-wide news + SEC daily filing index (can introduce new stocks)
  4. Screening (Stage A)  eligibility thresholds + financial / valuation / completeness scores
  5. Selection            25 financial leaders, 15 valuation opportunities, 15 new catalysts,
                          5 underexplored; deduped, refilled, seeded tie-breaks
  6. Deep analysis        existing quantitative engine (no AI) for the 60 candidates
  7. AI                   top 10 only; forecasts and scores recomputed afterwards
  8. Publish              up to 20 that meet the minimum score; full audit for every security
"""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from app.config import DataMode, get_settings
from app.models import (
    Analysis,
    DiscoveryCandidate,
    DiscoveryRun,
    Event,
    EventCompanyRelationship,
    Forecast,
    Recommendation,
    Security,
)
from app.services.ai import PROMPT_VERSION
from app.services.analysis import AnalysisError, analyze_ticker
from app.services.discovery import catalyst_score, company_events
from app.services.discovery_scoring import FORMULA, ScoreResult, band_for, compute_score, factor_values
from app.services.filings_index import ingest_filing_indexes
from app.services.forecasting.engine import MODEL_VERSION
from app.services.forecasting.valuation_guardrails import ranking_exclusion
from app.services.http import ProviderError, call_delta, call_snapshot
from app.services.market_calendar import NY
from app.services.market_screen import (
    ScreenResult,
    _json_safe,
    coverage_stats,
    persist_screen,
    refresh_fundamentals,
    refresh_market_bars,
    screen,
    size_bucket,
)
from app.services.master_universe import (
    company_for_security,
    make_resolver,
    refresh_master_universe,
    universe_version,
)
from app.services.screen_coverage import refresh_fundamentals_fallback, refresh_market_splits
from app.services.selection import UniverseItem, select_candidates
from app.services.universe import ensure_universe_companies, universe_tickers, watchlist_tickers
from app.services.universe_news import ingest_universe_news

log = logging.getLogger(__name__)

SCORING_VERSION = "score-2.0"
SELECTION_VERSION = "dynamic-1.0"
NEWLY_ELIGIBLE_DAYS = 14
UNDEREXPLORED_MIN_COMPLETENESS = 0.6
UNDEREXPLORED_STALE_DAYS = 30
CATALYST_WINDOW = timedelta(days=14)

REASON_TEXT = {
    "financial_leader": "Strong fundamentals (top financial-strength screen)",
    "valuation_opportunity": "Attractive sector-relative valuation (earnings / FCF / EBIT yield)",
    "new_catalyst": "Newly detected material catalyst",
    "underexplored": "Previously underexplored stock (not recently analyzed)",
    "refill": "Filled an unused pathway slot (next-best fundamentals)",
}


def dynamic_quotas() -> dict[str, int]:
    s = get_settings()
    return {"financial_leader": s.dynamic_quota_financial, "valuation_opportunity": s.dynamic_quota_valuation,
            "new_catalyst": s.dynamic_quota_catalyst, "underexplored": s.dynamic_quota_underexplored}


NOT_NEW_CATALYST_CLAIMS = {"estimated", "scheduled"}  # calendar entries are not newly detected events


def security_catalysts(session: Session, mode: DataMode, mcaps: dict[int, float | None], now: datetime
                       ) -> dict[int, tuple[float, Event | None, datetime | None, int, float]]:
    """security_id -> (catalyst score, best event, newest *detected* event time, recent event count,
    detected-event score). Estimated/scheduled calendar entries count toward catalyst importance
    but never as a "newly detected" catalyst (they're only computed for monitored companies)."""
    sec_by_company = dict(session.execute(select(Security.company_id, Security.id).where(
        Security.data_mode == mode, Security.company_id.is_not(None))).all())
    rows = session.execute(
        select(Event, EventCompanyRelationship).join(EventCompanyRelationship).where(
            Event.data_mode == mode, EventCompanyRelationship.company_id.in_(list(sec_by_company)),
            ((Event.last_published_at >= now - CATALYST_WINDOW) & Event.event_date.is_(None))
            | ((Event.event_date >= now.date()) & (Event.event_date <= now.date() + timedelta(days=60))))).all()
    grouped: dict[int, list] = defaultdict(list)
    for ev, link in rows:
        grouped[sec_by_company[link.company_id]].append((ev, link))
    out = {}
    for sid, evs in grouped.items():
        score, best = catalyst_score(evs, mcaps.get(sid), now)
        detected = [(e, lk) for e, lk in evs if (e.claim_type or "") not in NOT_NEW_CATALYST_CLAIMS
                    and e.event_type != "economic_release"]
        new_score, new_best = catalyst_score(detected, mcaps.get(sid), now) if detected else (0.0, None)
        out[sid] = (score, new_best or best, max((e.created_at for e, _ in detected), default=None), len(evs),
                    new_score)
    return out


def run_dynamic_discovery(session: Session, mode: DataMode, progress=lambda m: None) -> DiscoveryRun:
    s = get_settings()
    t0 = time.monotonic()
    api0 = call_snapshot()
    now = datetime.now(UTC)
    seed = now.astimezone(NY).date().isoformat()
    quotas = dynamic_quotas()
    prev = session.scalar(select(DiscoveryRun).where(DiscoveryRun.data_mode == mode, DiscoveryRun.status == "succeeded",
                                                     DiscoveryRun.universe_mode == "dynamic")
                          .order_by(DiscoveryRun.started_at.desc()))
    new_since = prev.started_at if prev else now - timedelta(hours=72)
    run = DiscoveryRun(data_mode=mode, seed=seed, universe_mode="dynamic", config={
        "formula": FORMULA, "quotas": quotas, "total_candidates": s.discovery_max_candidates,
        "top_n": s.discovery_top_n, "ai_top_n": s.discovery_ai_top_n,
        "min_publish_score": s.discovery_min_publish_score, "new_catalyst_since": new_since.isoformat(),
        "thresholds": {"min_market_cap": s.screen_min_market_cap, "min_avg_dollar_volume": s.min_avg_dollar_volume,
                       "max_price_staleness": s.screen_max_price_staleness,
                       "min_price_sessions": s.screen_min_price_sessions,
                       "catalyst_min_market_cap": s.catalyst_min_market_cap,
                       "catalyst_min_avg_dollar_volume": s.catalyst_min_avg_dollar_volume,
                       "catalyst_min_materiality": s.catalyst_min_materiality},
        "versions": {"forecast_model": MODEL_VERSION, "scoring": SCORING_VERSION, "selection": SELECTION_VERSION,
                     "ai_prompt": PROMPT_VERSION},
    })
    session.add(run)
    session.commit()
    notes: dict[str, Any] = {"errors": []}
    timings: dict[str, float] = {}

    def stage(name: str, fn, optional: bool = False):
        t = time.monotonic()
        try:
            return fn()
        except ProviderError as exc:
            notes["errors"].append(f"{name}: {exc.message}")
            session.rollback()
            return {"error": exc.message}
        except Exception as exc:  # optional data-coverage stages never abort the run
            if not optional:
                raise
            log.exception("discovery stage %s failed", name)
            notes["errors"].append(f"{name}: {type(exc).__name__}: {str(exc)[:200]}")
            session.rollback()
            return {"error": type(exc).__name__}
        finally:
            timings[name] = round(time.monotonic() - t, 1)

    # 1. master universe (weekly, or when empty)
    last = session.scalar(select(func.max(Security.last_refreshed_at)).where(Security.data_mode == mode))
    if last is None or now - last > timedelta(days=s.universe_refresh_days):
        notes["universe"] = stage("universe", lambda: refresh_master_universe(session, mode, progress))
    else:
        notes["universe"] = {"skipped": f"refreshed {last:%Y-%m-%d %H:%M} UTC"}
    # 2. market data + fundamentals (incremental)
    notes["prices"] = stage("prices", lambda: refresh_market_bars(session, mode, progress))
    notes["fundamentals"] = stage("fundamentals", lambda: refresh_fundamentals(session, mode, progress))
    notes["splits"] = stage("splits", lambda: refresh_market_splits(session, mode), optional=True)
    notes["fundamentals_fallback"] = stage("fundamentals_fallback",
                                           lambda: refresh_fundamentals_fallback(session, mode, progress), optional=True)
    # 3. events: market-wide news + SEC filing indexes (resolver links never-monitored stocks)
    resolver, known = make_resolver(session, mode)
    monitored = ensure_universe_companies(
        session, mode, list(dict.fromkeys(watchlist_tickers(session, mode) + universe_tickers(mode))))
    notes["news"] = stage("news", lambda: ingest_universe_news(session, mode, monitored, progress=progress,
                                                               resolver=resolver, known_tickers=known))
    notes["filings"] = stage("filings", lambda: ingest_filing_indexes(session, mode, progress))
    session.commit()

    # 4. catalysts + screening
    progress("Screening the market")
    mcaps = dict(session.execute(select(Security.id, Security.market_cap).where(Security.data_mode == mode)).all())
    cats = security_catalysts(session, mode, mcaps, now)
    t = time.monotonic()
    results = screen(session, mode, {sid: c[4] for sid, c in cats.items()}, progress)  # detected events only
    persist_screen(session, results)
    timings["screening"] = round(time.monotonic() - t, 1)
    # refresh catalysts with the newly computed market caps (significance factor)
    mcaps = {r.security_id: r.market_cap for r in results}
    cats = security_catalysts(session, mode, mcaps, now)

    # 5. selection
    progress("Selecting candidates")
    secs = {x.id: x for x in session.scalars(select(Security).where(Security.id.in_([r.security_id for r in results])))}
    last_analyzed = dict(session.execute(select(Analysis.company_id, func.max(Analysis.created_at))
                                         .where(Analysis.data_mode == mode).group_by(Analysis.company_id)).all())
    prior = session.execute(
        select(DiscoveryCandidate.security_id, DiscoveryCandidate.company_id)
        .join(DiscoveryRun, DiscoveryRun.id == DiscoveryCandidate.run_id)
        .where(DiscoveryRun.data_mode == mode, DiscoveryCandidate.selected, DiscoveryRun.id != run.id)).all()
    prior_companies = {c for _, c in prior if c}
    # candidate history from both dynamic runs (securities) and older fixed-universe runs (companies)
    ever = {sid for sid, _ in prior if sid} | {x.id for x in secs.values() if x.company_id in prior_companies}
    news_checked = _coverage_checked(notes)
    items: list[UniverseItem] = []
    for r in results:
        sec = secs[r.security_id]
        cat = cats.get(r.security_id)
        status = "detected" if cat else ("none_detected" if news_checked else "unknown")
        items.append(UniverseItem(
            ticker=sec.ticker, eligible=r.status in ("eligible", "catalyst_eligible"), security_id=sec.id,
            company_id=sec.company_id, ineligible_reason=r.reason, financial_score=r.financial_score,
            valuation_score=r.valuation_score, completeness=r.completeness, freshness=r.freshness,
            catalyst_status=status, catalyst_score=cat[4] if cat else None, catalyst_first_seen=cat[2] if cat else None,
            last_analyzed_at=last_analyzed.get(sec.company_id) if sec.company_id else None,
            newly_eligible=bool(sec.first_eligible_at and now - sec.first_eligible_at <= timedelta(days=NEWLY_ELIGIBLE_DAYS)),
            ever_candidate=sec.id in ever,
        ))
    sel = select_candidates(items, seed, new_since, total=s.discovery_max_candidates, quotas=quotas,
                            underexplored_min_completeness=UNDEREXPLORED_MIN_COMPLETENESS,
                            underexplored_stale_before=now - timedelta(days=UNDEREXPLORED_STALE_DAYS))

    # 6. audit rows for every screened security (point-in-time inputs), bulk insert
    order = {t: n for n, t in enumerate(sel.tickers, 1)}
    by_ticker = {i.ticker: i for i in items}
    res_by_ticker = {secs[r.security_id].ticker: r for r in results}
    rows = []
    for it in items:
        r = res_by_ticker[it.ticker]
        cat = cats.get(it.security_id)
        sel_flag = it.ticker in order
        rows.append(dict(
            run_id=run.id, company_id=it.company_id, security_id=it.security_id, ticker=it.ticker,
            status="selected" if sel_flag else ("not_selected" if it.eligible else "ineligible"),
            selected=sel_flag, selection_order=order.get(it.ticker), pathway=sel.pathway_of(it.ticker),
            qualifying_pathways=sel.qualifying.get(it.ticker, []), baseline_score=it.financial_score,
            catalyst_status=it.catalyst_status, catalyst_score=it.catalyst_score,
            previously_analyzed=it.last_analyzed_at is not None, last_analyzed_at=it.last_analyzed_at,
            tie_break_key=sel.tie_keys.get(it.ticker), eligibility_status=r.status, market_cap=r.market_cap,
            screen_metrics=_json_safe({**r.metrics, "financial_score": r.financial_score,
                                       "valuation_score": r.valuation_score, "size": size_bucket(r.market_cap),
                                       "sector": secs[it.security_id].sector, "industry": secs[it.security_id].industry,
                                       "coverage": r.coverage, "catalyst_events_14d": cat[3] if cat else 0,
                                       "exclusion_codes": r.codes if not it.eligible else []}),
            valuation_score=r.valuation_score, data_completeness=r.completeness,
            exclusion_reason=r.reason if not it.eligible else None,
            reasons=_reasons(it, r, sel, cat) if sel_flag else None,
            ai_status="not_selected" if not sel_flag else "pending",
            data_timestamps={"price_date": r.metrics.get("price_date"), "screened_at": now.isoformat(),
                             "catalyst_first_seen": cat[2].isoformat() if cat and cat[2] else None,
                             "universe_refreshed_at": (sec_last.isoformat() if (sec_last := secs[it.security_id].last_refreshed_at) else None)},
            model_versions=run.config["versions"],
        ))
    for i in range(0, len(rows), 1000):
        session.execute(insert(DiscoveryCandidate), rows[i:i + 1000])
    session.commit()
    cand = {c.ticker: c for c in session.scalars(select(DiscoveryCandidate).where(
        DiscoveryCandidate.run_id == run.id, DiscoveryCandidate.selected))}

    # 7. deep quantitative analysis (no AI) for the selected candidates
    evaluated: list[dict[str, Any]] = []
    t = time.monotonic()
    for n, tk in enumerate(sel.tickers, 1):
        progress(f"Analyzing {tk} ({n}/{len(sel.tickers)})")
        row = cand[tk]
        sec = secs[by_ticker[tk].security_id]
        try:
            comp = company_for_security(session, sec)
            row.company_id = comp.id
            session.commit()
            out = analyze_ticker(session, comp.ticker, mode, use_ai=False)
            session.commit()
            e = _evaluate(session, row, session.get(Analysis, out.analysis_id), by_ticker[tk].catalyst_status, s)
            if e:
                evaluated.append(e)
        except (AnalysisError, ProviderError) as exc:
            session.rollback()
            row = session.get(DiscoveryCandidate, row.id)
            row.status, row.exclusion_reason = "excluded", f"limited analysis: {exc}"
        except Exception as exc:
            session.rollback()
            log.exception("dynamic discovery failed for %s", tk)
            row = session.get(DiscoveryCandidate, row.id)
            row.status, row.exclusion_reason = "excluded", f"error: {type(exc).__name__}: {exc}"
        session.commit()
    timings["deep_analysis"] = round(time.monotonic() - t, 1)

    def key(e: dict[str, Any]) -> tuple:  # full precision; seeded tie-break, never ticker name
        sc: ScoreResult = e["score"]
        return (-(sc.exact if sc.exact is not None else -1), -(sc.factors.get("financial_strength") or 0),
                e["row"].tie_break_key or "")

    evaluated.sort(key=key)

    # 8. AI for the top N only, then recompute forecasts + scores
    ai_done = 0
    t = time.monotonic()
    for e in evaluated:
        e["row"].ai_status = "not_top_n"
    if ai_enabled(mode):
        for e in evaluated[: s.discovery_ai_top_n]:
            progress(f"AI analysis {e['ticker']}")
            try:
                out = analyze_ticker(session, e["company_ticker"], mode, use_ai=True)
                session.commit()
                a = session.get(Analysis, out.analysis_id)
                e2 = _evaluate(session, e["row"], a, e["catalyst_status"], s)
                if e2:
                    e.update(e2)
                e["row"].ai_status = "completed" if a.ai_kind == "openai" else "skipped"
                if a.ai_kind != "openai":
                    e["row"].factors = {**(e["row"].factors or {}), "ai_skip_reason":
                                        (a.usage or {}).get("skipped") or (a.usage or {}).get("error")}
                ai_done += a.ai_kind == "openai"
            except Exception as exc:
                session.rollback()
                e["row"].ai_status = "error"
                notes["errors"].append({"ticker": e["ticker"], "error": f"AI pass: {exc}"})
            session.commit()
    else:
        for e in evaluated[: s.discovery_ai_top_n]:
            e["row"].ai_status = "not_configured" if mode == "live" else "demo_mode"
    timings["ai"] = round(time.monotonic() - t, 1)
    evaluated.sort(key=key)

    # 9. publish up to top_n that meet the minimum score
    published = 0
    for rank, e in enumerate(evaluated, 1):
        row, sc = e["row"], e["score"]
        row.final_rank = rank
        if published >= s.discovery_top_n or (sc.exact or 0) < s.discovery_min_publish_score:
            if (sc.exact or 0) < s.discovery_min_publish_score:
                row.exclusion_reason = f"score {sc.exact:.1f} below publish minimum {s.discovery_min_publish_score:.0f}"
            continue
        published += 1
        row.status = "ranked"
        a: Analysis = e["analysis"]
        f12 = session.scalar(select(Forecast).where(Forecast.analysis_id == a.id, Forecast.horizon_months == 12))
        ev = e["catalyst"]
        session.add(Recommendation(
            run_id=run.id, company_id=a.company_id, analysis_id=a.id, forecast_id=f12.id if f12 else None,
            rank=published, score=sc.exact, score_band=band_for(sc.score), factors=sc.to_dict(),
            outlook=(a.snapshot or {}).get("outlook", "neutral"), risk_rating=(a.snapshot or {}).get("risk_rating", "Medium"),
            horizon_class=e["horizon"], catalyst_event_id=ev.id if ev else None, catalyst_title=ev.title if ev else None,
            catalyst_date=ev.event_date if ev else None, selection_pathway=row.pathway,
        ))
    session.commit()

    all_rows = list(session.scalars(select(DiscoveryCandidate).where(DiscoveryCandidate.run_id == run.id)))
    run.universe_version = universe_version(session, mode)
    run.api_calls = call_delta(api0)
    run.stats = _stats(session, mode, all_rows, sel, results, cats, ai_done, published, timings,
                       round(time.monotonic() - t0, 1))
    run.universe_size = len(results)
    run.candidates_evaluated = len(sel.tickers)
    run.status = "succeeded"
    run.finished_at = datetime.now(UTC)
    run.notes = notes
    session.commit()
    return run


def ai_enabled(mode: DataMode) -> bool:
    """Deep AI analysis runs only in live mode with an OpenAI key (and only for the top N)."""
    return mode == "live" and get_settings().openai_configured


def _coverage_checked(notes: dict[str, Any]) -> bool:
    """News coverage counts as checked when the market-wide feed covered at least ~24h."""
    return (notes.get("news") or {}).get("market_window_hours", 0) >= 24


def _evaluate(session: Session, row: DiscoveryCandidate, a: Analysis, cat_status: str, s) -> dict[str, Any] | None:
    snap = a.snapshot or {}
    row.analysis_id = a.id
    adv = (snap.get("price_stats") or {}).get("avg_dollar_volume_20d")
    reason = None
    if adv is not None and adv < s.catalyst_min_avg_dollar_volume:
        reason = f"illiquid (20d avg dollar volume {adv:,.0f})"
    elif (excl := ranking_exclusion(a.valuation_status, a.valuation)) is not None:
        reason = excl
    elif snap.get("base_target_12m") is None:
        reason = "limited analysis: no valid valuation (insufficient/implausible data or no applicable method)"
    vinfo = {"valuation_status": a.valuation_status, "valuation_method": (a.valuation or {}).get("method")}
    if reason:
        row.status, row.exclusion_reason = "excluded", reason
        row.factors = {**(row.factors or {}), **vinfo}
        return None
    evs = company_events(session, a.company_id)
    cat, cat_ev = catalyst_score(evs, row.market_cap)
    lf = snap.get("latest_filing_date")
    age = (date.today() - date.fromisoformat(lf)).days if lf else None
    result = compute_score(factor_values(snap, cat_status if not evs else "detected", cat, age))
    known = sum(1 for v in result.factors.values() if v is not None)
    row.status, row.score, row.factors = "screened", result.exact, {**result.to_dict(), "data_completeness": known / 6,
                                                                    **vinfo}
    soon = any(e.event_date and e.event_date <= date.today() + timedelta(days=60) and (e.materiality or 0) >= 0.5
               for e, _ in evs)
    recent_material = cat_ev is not None and cat_ev.event_date is None and cat >= 0.5
    return {"ticker": row.ticker, "company_ticker": a.company.ticker, "row": row, "analysis": a, "score": result,
            "catalyst": cat_ev, "catalyst_status": cat_status,
            "horizon": "short_term" if (soon or recent_material) else "long_term"}


def _reasons(it: UniverseItem, r: ScreenResult, sel, cat) -> list[dict[str, Any]]:
    out = [{"code": p, "text": REASON_TEXT[p]} for p in sel.qualifying.get(it.ticker, [])]
    filled = sel.pathway_of(it.ticker)
    if filled == "refill":
        out.append({"code": "refill", "text": REASON_TEXT["refill"]})
    if cat and cat[1] is not None and "new_catalyst" in sel.qualifying.get(it.ticker, []):
        ev: Event = cat[1]
        out[[o["code"] for o in out].index("new_catalyst")]["text"] = (
            f"New catalyst: {ev.title} ({(ev.claim_type or ev.status).replace('_', ' ')}, {ev.event_type.replace('_', ' ')})")
        out[[o["code"] for o in out].index("new_catalyst")]["source_url"] = ev.primary_url
    if r.status == "catalyst_eligible":
        out.append({"code": "catalyst_eligible", "text": "Below the size/liquidity minimum; qualified through an exceptional catalyst"})
    if it.newly_eligible and not it.ever_candidate:
        out.append({"code": "newly_eligible", "text": "Newly eligible company"})
    if not it.ever_candidate:
        out.append({"code": "first_time", "text": "First time in the candidate pool"})
    return out


def _stats(session: Session, mode: DataMode, rows: list[DiscoveryCandidate], sel, results: list[ScreenResult],
           cats: dict, ai_done: int, published: int, timings: dict, seconds: float) -> dict[str, Any]:
    status_counts = Counter(r.status for r in results)
    sel_rows = [r for r in rows if r.selected]
    ranked = [r for r in rows if r.status == "ranked"]
    excl = Counter(_reason_bucket(r.reason) for r in results if r.status not in ("eligible", "catalyst_eligible"))
    return {
        "mode": "dynamic",
        "total_discovered": session.query(Security).filter(Security.data_mode == mode).count(),
        "active_listed": session.query(Security).filter(Security.data_mode == mode, Security.active).count(),
        "excluded_types": session.query(Security).filter(Security.data_mode == mode,
                                                         Security.eligibility_status == "excluded_type").count(),
        "screened": len(results),
        "eligible": status_counts["eligible"] + status_counts["catalyst_eligible"],
        "catalyst_eligible": status_counts["catalyst_eligible"],
        "needs_alt_fundamentals": status_counts["needs_alt_fundamentals"],
        "valid_financials": sum(1 for r in results if r.coverage == "sec_full"),
        "recent_news_coverage": sum(1 for c in cats.values() if c[3] > 0),
        "selected": len(sel_rows),
        "pathway_slots": dict(sel.filled),
        "newly_discovered": sum(1 for r in sel_rows if any(x.get("code") == "first_time" for x in (r.reasons or []))),
        "previously_unanalyzed_selected": sum(1 for r in sel_rows if not r.previously_analyzed),
        "candidates_analyzed": sum(1 for r in sel_rows if r.analysis_id),
        "excluded_after_analysis": sum(1 for r in sel_rows if r.status == "excluded"),
        "ai_completed": ai_done,
        "published": published,
        "ranked": len(ranked),
        "selected_sectors": dict(Counter((r.screen_metrics or {}).get("sector") or "Unknown" for r in sel_rows)),
        "ranked_sectors": dict(Counter((r.screen_metrics or {}).get("sector") or "Unknown" for r in ranked)),
        "exclusion_reasons": dict(excl.most_common(12)),
        "coverage_metrics": {"securities_discovered": session.query(Security).filter(Security.data_mode == mode).count(),
                             **coverage_stats(results)},
        "valuation_statuses": dict(Counter((r.factors or {}).get("valuation_status") or "not_analyzed"
                                           for r in sel_rows)),
        "coverage": dict(Counter(r.catalyst_status for r in rows)),
        "timings_seconds": timings,
        "processing_seconds": seconds,
    }


def _reason_bucket(reason: str | None) -> str:
    if not reason:
        return "unknown"
    for k in ("insufficient price history", "stale price", "no usable SEC", "insufficient financial data",
              "market cap unknown", "liquidity unknown", "market cap", "illiquid", "ADR/foreign"):
        if reason.startswith(k):
            return k
    return reason[:40]
