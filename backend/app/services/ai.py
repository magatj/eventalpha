"""OpenAI-assisted research with structured (Pydantic-validated) outputs.

Guardrails
  * The model receives numbered sources [S1..Sn] and must cite them; citations to unknown
    sources are stripped, and fact-type claims left without a valid source are rejected.
  * The output schema has NO price/target fields — price targets come only from the
    quantitative engine. The model may suggest small, bounded growth/margin adjustments.
  * Results are cached by input hash; a monthly USD budget is enforced before each call.
  * Without an API key (or over budget) a deterministic, clearly-labeled rule-based
    summary is produced instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AIUsage

log = logging.getLogger(__name__)

PROMPT_VERSION = "thesis-v1"
EVENT_PROMPT_VERSION = "event-v1"

ClaimType = Literal["verified_fact", "management_guidance", "analyst_estimate", "model_assumption", "speculative"]
FACT_TYPES = {"verified_fact", "management_guidance", "analyst_estimate"}


class Claim(BaseModel):
    text: str
    claim_type: ClaimType
    source_ids: list[str]


class AssumptionSuggestion(BaseModel):
    scenario: Literal["bear", "base", "bull"]
    revenue_growth_adjustment_pp: float
    net_margin_adjustment_pp: float
    rationale: str
    source_ids: list[str]


class StockThesis(BaseModel):
    summary: str
    factual_events: list[Claim]
    bullish_arguments: list[Claim]
    bearish_arguments: list[Claim]
    expected_catalysts: list[Claim]
    business_opportunities: list[Claim]
    competitive_risks: list[Claim]
    valuation_risks: list[Claim]
    principal_risks: list[Claim]
    contradictory_evidence: list[Claim]
    revenue_drivers: list[Claim]
    margin_implications: list[Claim]
    short_term_outlook: str
    long_term_outlook: str
    outlook: Literal["bullish", "neutral", "bearish"]
    assumption_suggestions: list[AssumptionSuggestion]
    data_gaps: list[str]


class CompanyImpact(BaseModel):
    ticker: str
    relationship: str
    operational_impact: str
    direction: Literal["positive", "negative", "uncertain"]
    time_frame: Literal["near_term", "long_term", "both"]
    relationship_confidence: Literal["low", "medium", "high"]
    source_ids: list[str]


class EventInterpretation(BaseModel):
    factual_summary: str
    is_confirmed: bool
    affected_companies: list[CompanyImpact]
    revenue_drivers: list[Claim]
    margin_implications: list[Claim]
    risk_factors: list[Claim]
    near_term_implications: str
    long_term_implications: str
    contradictory_evidence: list[Claim]


SYSTEM_PROMPT = """You are a careful equity research assistant. Use ONLY the numbered sources provided.
Rules:
- Every claim must cite source ids like "S1" in source_ids. Never invent sources, URLs, numbers, prices or dates.
- Classify each claim: verified_fact (stated in a source), management_guidance (company outlook in a source),
  analyst_estimate (third-party estimate in a source), model_assumption (your reasoned assumption), speculative.
- Treat reports citing anonymous sources, rumors or "in talks" language as speculative, not fact.
- Do NOT state or estimate stock prices, price targets or fair values. Valuation is done by a separate quantitative engine.
- assumption_suggestions: optional small adjustments (in decimal percentage points, e.g. 0.02 = +2pp, max ±0.05 growth,
  ±0.03 margin) to the engine's baseline growth/margin, only when a cited source justifies it. Use 0 when not justified.
- Point out contradictory evidence and data gaps explicitly. Be concise and specific."""

EVENT_SYSTEM_PROMPT = """You analyze a single market event for equity research using ONLY the provided sources.
Summarize the factual event, list affected publicly traded companies (from the candidate list when possible),
explain each relationship, operational impact, revenue drivers, margin implications, risks, and near- vs long-term
implications. Cite source ids. Mark unconfirmed reports as not confirmed. Never invent numbers, prices or sources."""


# ---------------------------------------------------------------------------------
def month_spend(session: Session, now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return float(session.scalar(select(func.coalesce(func.sum(AIUsage.cost_usd), 0.0)).where(AIUsage.created_at >= start)) or 0.0)


def estimate_cost(settings: Settings, input_chars: int, max_output_tokens: int = 4000) -> float:
    return (input_chars / 4) / 1e6 * settings.openai_input_price_per_m + max_output_tokens / 1e6 * settings.openai_output_price_per_m


def _cache_key(model: str, version: str, payload: Any) -> str:
    return hashlib.sha256(f"{model}|{version}|{json.dumps(payload, sort_keys=True, default=str)}".encode()).hexdigest()


def validate_citations(thesis: dict[str, Any], valid_ids: set[str]) -> tuple[dict[str, Any], list[dict]]:
    """Strip unknown source ids; reject fact-type claims that end up without a valid citation."""
    rejected: list[dict] = []

    def fix_claims(claims: list[dict]) -> list[dict]:
        kept = []
        for c in claims:
            ids = [s for s in c.get("source_ids", []) if s in valid_ids]
            bad = [s for s in c.get("source_ids", []) if s not in valid_ids]
            c = {**c, "source_ids": ids}
            if bad:
                c["removed_source_ids"] = bad
            if c.get("claim_type") in FACT_TYPES and not ids:
                rejected.append({**c, "reason": "fact-type claim without a valid source"})
                continue
            if re.search(r"price target|target price|fair value of \$", c.get("text", ""), re.I):
                c["warning"] = "Mentions a price target; ignored by valuation (targets are computed quantitatively)."
            kept.append(c)
        return kept

    out = dict(thesis)
    for k, v in thesis.items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "claim_type" in v[0]:
            out[k] = fix_claims(v)
    if "assumption_suggestions" in out:
        out["assumption_suggestions"] = [
            {**a, "source_ids": [s for s in a["source_ids"] if s in valid_ids]}
            for a in out["assumption_suggestions"]
            if any(s in valid_ids for s in a.get("source_ids", []))
        ]
    if "affected_companies" in out:
        out["affected_companies"] = [{**c, "source_ids": [s for s in c["source_ids"] if s in valid_ids]}
                                     for c in out["affected_companies"]]
    return out, rejected


class AIEngine:
    def __init__(self, settings: Settings, session: Session, client: Any | None = None):
        self.s = settings
        self.session = session
        self._client = client

    @property
    def available(self) -> bool:
        return bool(self.s.openai_api_key) or self._client is not None

    def client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.s.openai_api_key, timeout=90, max_retries=2)
        return self._client

    def budget_status(self) -> dict[str, float]:
        spent = month_spend(self.session)
        return {"spent_usd": round(spent, 4), "budget_usd": self.s.ai_monthly_budget_usd,
                "remaining_usd": round(max(0.0, self.s.ai_monthly_budget_usd - spent), 4)}

    def _cached(self, key: str, ttl_hours: int) -> dict | None:
        row = self.session.scalar(
            select(AIUsage).where(AIUsage.cache_key == key, AIUsage.response.is_not(None),
                                  AIUsage.created_at >= datetime.now(UTC) - timedelta(hours=ttl_hours))
            .order_by(AIUsage.created_at.desc())
        )
        return row.response if row else None

    def _call(self, purpose: str, system: str, user: str, schema: type[BaseModel], version: str,
              cache_payload: Any, force: bool = False) -> tuple[dict | None, dict]:
        """Returns (parsed_or_None, meta). meta explains cache hits/skips/costs."""
        model = self.s.openai_model
        key = _cache_key(model, version, cache_payload)
        meta: dict[str, Any] = {"model": model, "prompt_version": version, "cache_key": key}
        if not self.available:
            return None, {**meta, "skipped": "OPENAI_API_KEY not configured"}
        if not force:
            hit = self._cached(key, self.s.ai_cache_ttl_hours)
            if hit is not None:
                return hit, {**meta, "cache_hit": True, "cost_usd": 0.0}
        est = estimate_cost(self.s, len(system) + len(user))
        spent = month_spend(self.session)
        if spent + est > self.s.ai_monthly_budget_usd:
            return None, {**meta, "skipped": f"monthly AI budget reached (${spent:.2f} of ${self.s.ai_monthly_budget_usd:.2f})"}
        try:
            resp = self.client().responses.parse(
                model=model,
                input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                text_format=schema,
            )
        except Exception as exc:  # network/auth/model errors -> caller falls back
            log.warning("OpenAI call failed: %s", exc)
            return None, {**meta, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        parsed = resp.output_parsed
        if parsed is None:
            return None, {**meta, "error": "model returned no parseable structured output (possibly a refusal)"}
        usage = getattr(resp, "usage", None)
        it = int(getattr(usage, "input_tokens", 0) or 0)
        ot = int(getattr(usage, "output_tokens", 0) or 0)
        cost = it / 1e6 * self.s.openai_input_price_per_m + ot / 1e6 * self.s.openai_output_price_per_m
        data = parsed.model_dump()
        self.session.add(AIUsage(model=getattr(resp, "model", model) or model, purpose=purpose, cache_key=key,
                                 input_tokens=it, output_tokens=ot, cost_usd=cost, response=data))
        self.session.flush()
        return data, {**meta, "cache_hit": False, "input_tokens": it, "output_tokens": ot, "cost_usd": round(cost, 6),
                      "response_model": getattr(resp, "model", model)}

    def stock_thesis(self, ticker: str, company: str, facts_block: dict[str, Any], sources: list[dict],
                     force: bool = False) -> tuple[dict | None, dict]:
        payload = {"ticker": ticker, "company": company, "data": facts_block,
                   "sources": [{k: s.get(k) for k in ("id", "type", "title", "publisher", "published_at", "url", "excerpt")}
                               for s in sources[: self.s.ai_max_sources]]}
        user = ("Produce an evidence-based investment thesis for the company below.\n"
                "Quantitative data (from SEC filings/market data; cite the matching source id):\n"
                + json.dumps(payload, default=str, indent=1))
        data, meta = self._call("stock_thesis", SYSTEM_PROMPT, user, StockThesis, PROMPT_VERSION, payload, force)
        if data is None:
            return None, meta
        cleaned, rejected = validate_citations(data, {s["id"] for s in sources})
        meta["rejected_claims"] = rejected
        return cleaned, meta

    def interpret_event(self, event: dict[str, Any], candidates: list[dict], sources: list[dict]) -> tuple[dict | None, dict]:
        payload = {"event": event, "candidate_companies": candidates, "sources": sources}
        user = "Analyze this event:\n" + json.dumps(payload, default=str, indent=1)
        data, meta = self._call("event_interpretation", EVENT_SYSTEM_PROMPT, user, EventInterpretation,
                                EVENT_PROMPT_VERSION, payload)
        if data is None:
            return None, meta
        cleaned, rejected = validate_citations(data, {s["id"] for s in sources})
        meta["rejected_claims"] = rejected
        return cleaned, meta


# ---------------------------------------------------------------------------------
def _c(text: str, kind: ClaimType, ids: list[str]) -> dict:
    return {"text": text, "claim_type": kind, "source_ids": ids}


def rule_based_thesis(m: dict[str, Any], sources: list[dict], upcoming: list[dict], demo: bool) -> dict[str, Any]:
    """Deterministic summary built only from computed metrics. Every claim cites the data source."""
    fin = next((s["id"] for s in sources if s["type"] in ("sec_xbrl", "demo_financials")), None)
    mkt = next((s["id"] for s in sources if s["type"] in ("market_data", "demo_market")), None)
    fid = [fin] if fin else []
    pid = [mkt] if mkt else []
    bull, bear, risks, val_risks, catalysts, facts = [], [], [], [], [], []

    def pct(x: float | None) -> str:
        return "n/a" if x is None else f"{x:+.1%}"

    g = m.get("revenue_growth_yoy")
    if g is not None:
        facts.append(_c(f"Trailing-twelve-month revenue changed {pct(g)} year over year.", "verified_fact", fid))
        if g > 0.08:
            bull.append(_c(f"Revenue growth of {pct(g)} supports the earnings outlook.", "model_assumption", fid))
        elif g < 0:
            bear.append(_c(f"Revenue decline of {pct(g)} weighs on the earnings outlook.", "model_assumption", fid))
    nm = m.get("net_margin")
    if nm is not None:
        facts.append(_c(f"TTM net margin is {nm:.1%}.", "verified_fact", fid))
        if nm < 0:
            bear.append(_c("The company is unprofitable on a trailing basis; earnings-based valuation is unreliable.", "model_assumption", fid))
        elif nm > 0.2:
            bull.append(_c(f"High net margin ({nm:.1%}) indicates pricing power or operating leverage.", "model_assumption", fid))
    fcf = m.get("fcf_margin")
    if fcf is not None:
        (bull if fcf > 0.1 else bear if fcf < 0 else facts).append(
            _c(f"Free-cash-flow margin is {fcf:.1%} (TTM).", "verified_fact", fid))
    nc = m.get("net_cash")
    if nc is not None:
        (bull if nc > 0 else bear).append(_c(
            f"{'Net cash' if nc > 0 else 'Net debt'} of {abs(nc) / 1e9:.1f}B {'adds flexibility' if nc > 0 else 'adds financial risk'}.",
            "verified_fact" if nc > 0 else "model_assumption", fid))
    pe, band = m.get("pe_ttm"), m.get("pe_band")
    if pe and band:
        if pe > band[2]:
            val_risks.append(_c(f"Trailing P/E of {pe:.1f}x is above the bull-case multiple ({band[2]:.1f}x); multiple compression is a risk.", "model_assumption", fid + pid))
        elif pe < band[0]:
            bull.append(_c(f"Trailing P/E of {pe:.1f}x is below the bear-case multiple ({band[0]:.1f}x).", "model_assumption", fid + pid))
    vol = m.get("volatility_1y")
    if vol is not None:
        risks.append(_c(f"1-year annualized price volatility is {vol:.0%}.", "verified_fact", pid))
    for u in upcoming[:5]:
        catalysts.append(_c(f"{u['title']} ({u['status']}) on {u['event_date']}.",
                            "verified_fact" if u["status"] == "scheduled" else "model_assumption", [u["source_id"]] if u.get("source_id") else []))
    risks.append(_c("This summary is rule-based and does not interpret news content.", "model_assumption", []))
    outlook = "neutral"
    br = m.get("base_return_12m")
    if br is not None:
        outlook = "bullish" if br > 0.10 and (m.get("bear_return_12m") or -1) > -0.30 else "bearish" if br < -0.05 else "neutral"
    label = "DEMO synthetic data" if demo else "SEC filings and market data"
    return {
        "summary": f"Rule-based summary generated from {label} (no language model used).",
        "factual_events": facts, "bullish_arguments": [b for b in bull if b], "bearish_arguments": [b for b in bear if b],
        "expected_catalysts": catalysts, "business_opportunities": [], "competitive_risks": [],
        "valuation_risks": val_risks, "principal_risks": risks, "contradictory_evidence": [],
        "revenue_drivers": [], "margin_implications": [],
        "short_term_outlook": "Not assessed without AI interpretation of news; see upcoming catalysts.",
        "long_term_outlook": "Driven by the quantitative scenarios below.",
        "outlook": outlook, "assumption_suggestions": [],
        "data_gaps": ["News content not interpreted (OpenAI not used)."],
    }
