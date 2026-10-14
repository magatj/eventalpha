"""Grounded context for the research chat: only the selected ticker's stored EventAlpha records.

Nothing here calls a market-data provider or re-runs an analysis. Every block is built from stored
rows (analysis snapshot + valuation audit, immutable forecasts, SEC facts, events, discovery rows)
and every block that can be cited carries a source label (S1, S2, ...).

Retrieved news/filing text is untrusted: it is truncated, stripped of control characters, placed
under `untrusted_text` fields and flagged when it contains instruction-like phrases. The system
prompt tells the model that such text is data, never instructions.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
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
from app.services.market_calendar import trading_days_stale

INJECTION_RE = re.compile(
    r"(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions|prompts?|rules)"
    r"|you are now|system prompt|act as|new instructions|reveal (your|the) (prompt|instructions|key)"
    r"|api[_ ]?key|begin (developer|system) message", re.I)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

INTENTS: dict[str, tuple[str, ...]] = {
    "valuation": ("target", "forecast", "valuation", "p/e", "pe ", "multiple", "dcf", "assumption", "upside", "downside",
                  "price", "calculat", "base", "bear", "bull", "worth", "fair value", "eps", "earnings", "what if",
                  "slow", "growth", "margin", "rated", "rating", "buy", "hold", "explain"),
    "events": ("news", "catalyst", "event", "upcoming", "filing", "8-k", "10-q", "10-k", "announce", "earnings date"),
    "discovery": ("rank", "score", "discovery", "pathway", "above", "below", "compared", "versus", " vs", "selected",
                  "why was", "rated", "rating", "buy", "hold"),
    "history": ("changed", "change since", "previous", "history", "last forecast", "earlier", "revised"),
    "risks": ("risk", "bad", "bear", "invalidate", "wrong", "downside", "concern", "weak", "threat"),
    "thesis": ("good", "bad", "invest", "thesis", "bull", "why", "strength", "opportunit"),
    "financials": ("revenue", "margin", "cash", "debt", "liquidity", "balance", "free cash", "fcf", "financial",
                   "profit", "income", "health"),
}
BLOCK_PRIORITY = ["company", "freshness", "valuation_summary", "key_financials", "valuation_detail", "risks",
                  "thesis", "events", "discovery", "history", "financial_history"]


def detect_intents(question: str) -> set[str]:
    q = f" {question.lower()} "
    out = {k for k, words in INTENTS.items() if any(w in q for w in words)}
    return out or {"valuation", "thesis", "events"}


def untrusted(text: str | None, limit: int = 400) -> dict[str, Any]:
    t = CONTROL_RE.sub(" ", (text or ""))[:limit]
    return {"untrusted_text": t, **({"possible_prompt_injection": True} if INJECTION_RE.search(t) else {})}


def _r(x: Any, d: int = 4) -> Any:
    if isinstance(x, float):
        return None if not math.isfinite(x) else round(x, d)
    return x


@dataclass
class ChatContext:
    ticker: str
    company: Company
    analysis: Analysis | None
    blocks: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    freshness: dict[str, Any] = field(default_factory=dict)
    intents: set[str] = field(default_factory=set)
    dropped_blocks: list[str] = field(default_factory=list)

    def add_source(self, stype: str, title: str, url: str | None = None, published: str | None = None,
                   ref: str | None = None) -> str:
        for s in self.sources:
            if s["ref"] == ref and ref:
                return s["label"]
        label = f"S{len(self.sources) + 1}"
        self.sources.append({"label": label, "type": stype, "title": title[:300], "url": safe_url(url),
                             "published_at": published, "ref": ref})
        return label

    def labels(self) -> set[str]:
        return {s["label"] for s in self.sources}

    def to_prompt(self) -> str:
        payload = {"ticker": self.ticker, "sources": [{k: s[k] for k in ("label", "type", "title", "published_at")}
                                                      for s in self.sources], **self.blocks}
        return json.dumps(payload, default=str, separators=(",", ":"))


def safe_url(url: str | None) -> str | None:
    """Only absolute http(s) URLs are kept (no javascript:, data:, relative or malformed links)."""
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    return u if re.match(r"^https?://[^\s<>\"']+$", u) else None


def latest_analysis(session: Session, company_id: int) -> Analysis | None:
    return session.scalar(select(Analysis).where(Analysis.company_id == company_id).order_by(Analysis.created_at.desc()))


def forecast_rows(session: Session, analysis_id: int) -> dict[int, Forecast]:
    return {f.horizon_months: f for f in session.scalars(select(Forecast).where(Forecast.analysis_id == analysis_id))}


def scenario_summary(f: Forecast) -> dict[str, Any]:
    out = {"horizon_months": f.horizon_months, "valuation_date": str(f.target_date),
           "reference_price": _r(f.observed_price, 2), "reference_price_date": str(f.observed_price_date),
           "expired": f.target_date < date.today()}
    for s in f.scenarios:
        out[s.scenario] = {"target_price": _r(s.target_price, 2), "return": _r(s.potential_return),
                           "eps": _r(s.eps), "revenue": _r(s.revenue, 0), "revenue_growth_annual": _r(s.revenue_growth),
                           "net_margin": _r(s.net_margin), "pe_multiple": _r(s.pe_multiple, 2),
                           "target_pe": _r(s.target_price_pe, 2), "target_dcf": _r(s.target_price_dcf, 2),
                           "target_other": _r(s.target_price_other, 2),
                           "method_weights": (s.details or {}).get("method_weights"),
                           "explanation": s.explanation}
    return out


def attribution(a: Analysis, f12: Forecast | None) -> dict[str, Any] | None:
    """Decompose the 12-month base target: EPS growth vs multiple change (P/E method), then method blending.

    P/E method: target_pe - price = (EPS1 - EPS0) x PE0  +  (PE1 - PE0) x EPS1, with PE0 = price / EPS0 —
    an exact additive split. Blended target = sum(weight_k x target_k)."""
    if f12 is None:
        return None
    base = next((s for s in f12.scenarios if s.scenario == "base"), None)
    if base is None or base.target_price is None:
        return {"available": False, "reason": "no base-case target (withheld or no applicable method)"}
    snap = a.snapshot or {}
    p0 = f12.observed_price
    det = base.details or {}
    out: dict[str, Any] = {"available": True, "reference_price": _r(p0, 2), "final_base_target": _r(base.target_price, 2),
                           "method_weights": det.get("method_weights"),
                           "method_targets": {k: _r(v, 2) for k, v in (det.get("method_targets") or {}).items()}}
    w = det.get("method_weights") or {}
    mt = det.get("method_targets") or {}
    out["blend"] = [{"method": k, "weight": _r(wt), "value": _r(mt.get(k), 2),
                     "contribution_to_target": _r(wt * mt[k], 2) if k in mt else None} for k, wt in w.items()]
    eps0, pe1 = snap.get("eps_ttm"), base.pe_multiple
    eps1 = det.get("normalized_eps") or base.eps
    if eps0 and eps0 > 0 and eps1 and pe1 and base.target_price_pe:
        pe0 = p0 / eps0
        growth = (eps1 - eps0) * pe0
        mult = (pe1 - pe0) * eps1
        out["pe_method"] = {
            "eps_today": _r(eps0), "eps_at_horizon": _r(eps1), "pe_today": _r(pe0, 2), "pe_at_horizon": _r(pe1, 2),
            "uses_normalized_eps": "normalized_eps" in det,
            "pe_target": _r(base.target_price_pe, 2),
            "from_eps_growth": _r(growth, 2), "from_multiple_change": _r(mult, 2),
            "check": "pe_target = reference_price + from_eps_growth + from_multiple_change",
            "share_of_pe_upside": {"eps_growth": _r(growth / (growth + mult)) if growth + mult else None,
                                   "multiple_change": _r(mult / (growth + mult)) if growth + mult else None},
        }
    else:
        out["pe_method"] = {"available": False, "reason": "P/E method not used or EPS not positive"}
    return out


def build_context(session: Session, mode: str, company: Company, question: str,
                  analysis: Analysis | None = None) -> ChatContext:
    s = get_settings()
    a = analysis or latest_analysis(session, company.id)
    ctx = ChatContext(company.ticker, company, a, intents=detect_intents(question))
    b = ctx.blocks
    b["company"] = {"ticker": company.ticker, "name": company.name, "sector": company.sector,
                    "industry": company.industry, "exchange": company.exchange,
                    "data_mode": mode + (" (synthetic DEMO data, not real)" if mode == "demo" else "")}
    if a is None:
        b["freshness"] = {"analysis": "none — this stock has not been analyzed yet"}
        ctx.freshness = {"has_analysis": False, "stale": True}
        return ctx

    snap = a.snapshot or {}
    v = a.valuation or {}
    fc = forecast_rows(session, a.id)
    f12 = fc.get(12)
    s_an = ctx.add_source("eventalpha_analysis", f"EventAlpha analysis #{a.id} ({a.forecast_model_version}, "
                          f"{a.created_at:%Y-%m-%d %H:%M} UTC)", None, a.created_at.isoformat(), f"analysis:{a.id}")
    s_px = ctx.add_source("market_data", f"{company.ticker} daily close {snap.get('price_date')}",
                          None, str(snap.get("price_date")), f"price:{snap.get('price_date')}")
    s_sec = None
    if mode == "live" and company.cik:
        s_sec = ctx.add_source("sec_xbrl", f"SEC XBRL company facts — {company.name}",
                               f"https://data.sec.gov/api/xbrl/companyfacts/CIK{company.cik.zfill(10)}.json",
                               snap.get("latest_filing_date"), f"sec:{company.cik}")
    price_date = date.fromisoformat(snap["price_date"]) if snap.get("price_date") else None
    stale_sessions = trading_days_stale(price_date) if price_date else None
    age_days = (datetime.now(UTC) - a.created_at).days
    ctx.freshness = {
        "has_analysis": True, "analysis_id": a.id, "analysis_created_at": a.created_at.isoformat(),
        "analysis_age_days": age_days, "price_date": snap.get("price_date"), "price_stale_sessions": stale_sessions,
        "latest_filing": snap.get("latest_filing_date"), "ttm_period_end": snap.get("ttm_period_end"),
        "forecast_model_version": a.forecast_model_version,
        "stale": bool((stale_sessions or 0) > 2 or age_days > s.chat_stale_analysis_days),
    }
    b["freshness"] = {**ctx.freshness, "note": "If stale, say so and suggest 'Refresh analysis' on the stock page."}
    b["valuation_summary"] = {
        "source": s_an, "status": a.valuation_status or "not assessed (pre-guardrail model version)",
        "status_meaning": {"valid": "supported by company-specific evidence", "limited_data": "usable but relies on fallbacks",
                           "requires_review": "NOT supported by its assumptions; excluded from rankings",
                           "unsupported_valuation": "no suitable method; no official target",
                           "invalid_inputs": "inconsistent source data; targets withheld"}.get(a.valuation_status or "", ""),
        "method": v.get("method_label"), "business_model": v.get("business_model"), "rationale": v.get("rationale"),
        "earnings_state": v.get("earnings_state"), "outlook_quant": snap.get("outlook"), "ai_outlook": snap.get("ai_outlook"),
        "risk_rating": snap.get("risk_rating"), "risk_reasons": snap.get("risk_reasons"),
        "reference_price": _r(snap.get("price"), 2), "price_source": s_px,
        "market_cap": _r((v.get("market_cap") or {}).get("value") or snap.get("market_cap"), 0),
        "market_cap_basis": (v.get("market_cap") or {}).get("basis") or snap.get("market_cap_basis"),
        "horizon_12m": scenario_summary(f12) if f12 else None,
        "other_horizons": {h: {sc.scenario: _r(sc.target_price, 2) for sc in f.scenarios} | {"valuation_date": str(f.target_date)}
                           for h, f in sorted(fc.items()) if h != 12},
        "warnings": (a.warnings or [])[:6],
        "rating_note": "EventAlpha does not issue Buy/Hold/Sell ratings; it reports a discovery score band "
                       "(Strong/Moderate/Weak), a quantitative outlook and a risk rating.",
    }
    b["key_financials"] = {"source": s_sec or s_an, **{k: _r(snap.get(k)) for k in (
        "revenue_ttm", "revenue_growth_yoy", "revenue_cagr_3y", "eps_ttm", "eps_growth_yoy", "pe_ttm", "net_margin",
        "operating_margin", "fcf_margin", "free_cash_flow_ttm", "cash", "debt", "net_cash", "beta", "volatility_1y")}}
    if ctx.intents & {"valuation", "risks"}:
        guard = v.get("guardrails") or {}
        b["valuation_detail"] = {
            "source": s_an, "attribution_12m_base": attribution(a, f12),
            "multiples": {k: _trim_sel(x) for k, x in (v.get("multiples") or {}).items() if x},
            "peers": (v.get("peers") or {}).get("level"),
            "guardrail_flags": [{"severity": f["severity"], "message": f["message"]} for f in guard.get("flags", [])],
            "upside_check": guard.get("upside_check"),
            "assumptions": [{"name": x.get("name"), "value": x.get("value"), "basis": (x.get("basis") or "")[:200],
                             "kind": x.get("kind")} for x in ((f12.assumptions or {}).get("items") or [])[:16]] if f12 else [],
            "dcf_note": (f12.valuation_methods or {}).get("dcf", {}).get("note") if f12 else None,
            "limitations": ["Targets are fundamental value estimates at the valuation date, not predictions of the market price.",
                            "Bear/base/bull ranges are not calibrated probabilities."] + (v.get("missing_inputs") or []),
        }
    thesis = a.thesis or {}
    if ctx.intents & {"thesis", "risks"}:
        b["thesis"] = {"source": s_an, "kind": a.ai_kind,
                       "bullish_points": snap.get("bullish_points", [])[:5], "bearish_points": snap.get("bearish_points", [])[:5],
                       "summary": (thesis.get("summary") or "")[:600],
                       "contradictory_evidence": [c.get("text") for c in thesis.get("contradictory_evidence", [])[:4]],
                       "data_gaps": thesis.get("data_gaps", [])[:5]}
    if "risks" in ctx.intents and f12:
        bear = next((x for x in f12.scenarios if x.scenario == "bear"), None)
        b["risks"] = {"source": s_an, "bear_case": bear.explanation if bear else None,
                      "risk_rating": snap.get("risk_rating"), "risk_reasons": snap.get("risk_reasons"),
                      "sensitivity_available": bool(f12.sensitivity)}
    if ctx.intents & {"events", "thesis", "risks"}:
        b["events"] = events_block(session, ctx, company.id, limit=6)
    if "discovery" in ctx.intents:
        b["discovery"] = discovery_block(session, ctx, mode, company.id)
    if "history" in ctx.intents:
        b["history"] = history_block(session, ctx, company.id, limit=5)
    if "financials" in ctx.intents:
        b["financial_history"] = {"source": s_sec or s_an, "annual": financial_history(session, company.id),
                                  "coverage": v.get("financial_coverage")}
    _fit(ctx, s.chat_max_context_tokens * 4)
    return ctx


def financial_history(session: Session, company_id: int, years: int = 6) -> list[dict[str, Any]]:
    """Annual revenue / margins / FCF from stored SEC facts (point-in-time snapshot as of today)."""
    from app.services.analysis import company_splits
    from app.services.financials import build_snapshot, load_facts

    snap = build_snapshot(load_facts(session, company_id), splits=company_splits(session, company_id))
    return [{k: _r(h.get(k)) for k in ("fiscal_year", "period_end", "revenue", "net_margin", "operating_margin",
                                       "fcf_margin", "roe")} for h in snap.margin_history[-years:]]


def _trim_sel(x: dict[str, Any]) -> dict[str, Any]:
    sel = x.get("multiples") if "multiples" in x and isinstance(x.get("multiples"), dict) else x
    keep = {k: sel.get(k) for k in ("kind", "current", "bear", "base_anchor", "bull", "basis", "limited")}
    ev = sel.get("evidence") or {}
    ch = ev.get("company_history") or {}
    pe = ev.get("peers") or {}
    keep["company_history"] = {k: ch.get(k) for k in ("p25", "p50", "p75", "n")} if ch else None
    keep["peers"] = {k: pe.get(k) for k in ("p25", "p50", "p75", "n")} if pe else None
    if x is not sel:  # book / FFO model wrapper
        keep.update({k: x.get(k) for k in ("measure", "per_share_today", "normalized_return", "return_basis",
                                            "justified_multiple", "ffo_per_share_today", "ffo_basis") if k in x})
    return keep


def events_block(session: Session, ctx: ChatContext, company_id: int, limit: int = 6, days: int = 30) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=days)
    rows = session.execute(select(Event, EventCompanyRelationship).join(EventCompanyRelationship).where(
        EventCompanyRelationship.company_id == company_id,
        (Event.last_published_at >= since) | (Event.event_date >= date.today()))
        .order_by(Event.event_date.is_(None), Event.event_date, Event.last_published_at.desc())).all()
    rows = sorted(rows, key=lambda r: (-(r[0].materiality or 0)))[:limit]
    out = []
    for ev, link in rows:
        label = ctx.add_source("sec_filing" if ev.event_type == "sec_filing" else "news", ev.title, ev.primary_url,
                               (ev.event_date or ev.last_published_at).isoformat() if (ev.event_date or ev.last_published_at) else None,
                               f"event:{ev.id}")
        out.append({"source": label, "type": ev.event_type, "status": ev.status, "claim_type": ev.claim_type,
                    "upcoming_date": str(ev.event_date) if ev.event_date else None,
                    "published": ev.last_published_at.isoformat() if ev.last_published_at else None,
                    "materiality": _r(ev.materiality, 2), "impact": ev.impact, "link_confidence": _r(link.confidence, 2),
                    "publisher": ev.source_name, "title": untrusted(ev.title, 200), "summary": untrusted(ev.summary, 400)})
    return out


def discovery_block(session: Session, ctx: ChatContext, mode: str, company_id: int,
                    compare_ticker: str | None = None) -> dict[str, Any]:
    run = session.scalar(select(DiscoveryRun).where(DiscoveryRun.data_mode == mode, DiscoveryRun.status == "succeeded")
                         .order_by(DiscoveryRun.started_at.desc()))
    if run is None:
        return {"available": False, "reason": "no discovery run yet"}
    label = ctx.add_source("eventalpha_discovery", f"EventAlpha discovery run #{run.id}", None,
                           run.finished_at.isoformat() if run.finished_at else None, f"run:{run.id}")

    def row_for(tk: str) -> dict[str, Any] | None:
        r = session.scalar(select(DiscoveryCandidate).where(DiscoveryCandidate.run_id == run.id,
                                                            DiscoveryCandidate.ticker == tk))
        if r is None:
            return None
        rec = session.scalar(select(Recommendation).join(Company).where(Recommendation.run_id == run.id, Company.ticker == tk))
        f = r.factors or {}
        return {"ticker": tk, "status": r.status, "eligibility": r.eligibility_status, "pathway": r.pathway,
                "qualifying_pathways": r.qualifying_pathways, "score": _r(r.score, 2), "published_rank": rec.rank if rec else None,
                "score_band": rec.score_band if rec else None, "factors": f.get("factors"), "contributions": f.get("contributions"),
                "unknown_factors": f.get("unknown_factors"), "valuation_status": f.get("valuation_status"),
                "reasons": [x.get("text") for x in (r.reasons or [])], "exclusion_reason": r.exclusion_reason}

    out = {"source": label, "run_id": run.id, "run_finished": run.finished_at.isoformat() if run.finished_at else None,
           "this_stock": row_for(ctx.ticker) or {"status": "not in the latest run"},
           "score_formula": "100 x (raw - worst)/(best - worst) over known factors: financial strength 25%, catalyst 20%, "
                            "valuation 25%, freshness 10%, minus forecast uncertainty 10% and downside risk 10%"}
    if compare_ticker:
        out["comparison"] = row_for(compare_ticker) or {"ticker": compare_ticker, "status": "not in the latest run"}
    return out


def history_block(session: Session, ctx: ChatContext, company_id: int, limit: int = 6) -> list[dict[str, Any]]:
    rows = list(session.scalars(select(Analysis).where(Analysis.company_id == company_id)
                                .order_by(Analysis.created_at.desc()).limit(limit)))
    out = []
    for a in rows:
        sn = a.snapshot or {}
        label = ctx.add_source("eventalpha_analysis", f"EventAlpha analysis #{a.id} ({a.forecast_model_version}, "
                               f"{a.created_at:%Y-%m-%d})", None, a.created_at.isoformat(), f"analysis:{a.id}")
        out.append({"source": label, "analysis_id": a.id, "created_at": a.created_at.isoformat(),
                    "model_version": a.forecast_model_version, "valuation_status": a.valuation_status,
                    "reference_price": _r(a.observed_price, 2), "price_date": str(a.observed_price_date),
                    "base_target_12m": _r(sn.get("base_target_12m"), 2), "bear_target_12m": _r(sn.get("bear_target_12m"), 2),
                    "bull_target_12m": _r(sn.get("bull_target_12m"), 2), "base_return_12m": _r(sn.get("base_return_12m")),
                    "outlook": sn.get("outlook"), "eps_ttm": _r(sn.get("eps_ttm")),
                    "financial_data_version": a.financial_data_version})
    return out


def _fit(ctx: ChatContext, max_chars: int) -> None:
    """Drop the lowest-priority blocks until the serialized context fits the budget."""
    for name in reversed(BLOCK_PRIORITY):
        if len(ctx.to_prompt()) <= max_chars:
            return
        if name in ctx.blocks and name not in ("company", "freshness", "valuation_summary"):
            del ctx.blocks[name]
            ctx.dropped_blocks.append(name)
