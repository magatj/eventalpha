"""Six-factor discovery score — explicit, bounded and testable.

Every factor is a number in [0, 1], or None when its inputs are unknown.

    positive factors P (weight):  financial_strength 0.25, catalyst_materiality 0.20,
                                  valuation 0.25, data_freshness 0.10
    penalty factors  N (weight):  forecast_uncertainty 0.10, downside_risk 0.10

    raw   = Σ_{p∈P, known} w_p·f_p  −  Σ_{n∈N, known} w_n·f_n
    best  = Σ_{p∈P, known} w_p            (all known positives = 1, all known penalties = 0)
    worst = −Σ_{n∈N, known} w_n           (all known positives = 0, all known penalties = 1)
    score = 100 · (raw − worst) / (best − worst)        → exactly 0..100
    reported score = round(score / 5) · 5, band: Strong ≥ 70, Moderate ≥ 50, else Weak

Unknown factors are dropped from raw, best and worst alike, i.e. the remaining weights are
renormalized. Missing information therefore neither rewards nor punishes a stock; in particular
a stock whose news has not been checked ("unknown" catalyst) is NOT scored as if it had a
zero-importance catalyst. A stock checked and found to have no catalyst scores catalyst = 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

POSITIVE_WEIGHTS: dict[str, float] = {
    "financial_strength": 0.25,
    "catalyst_materiality": 0.20,
    "valuation": 0.25,
    "data_freshness": 0.10,
}
PENALTY_WEIGHTS: dict[str, float] = {
    "forecast_uncertainty": 0.10,
    "downside_risk": 0.10,
}
FACTOR_WEIGHTS: dict[str, float] = {**POSITIVE_WEIGHTS, **{k: -w for k, w in PENALTY_WEIGHTS.items()}}
SCORE_STEP = 5
BANDS = ((70, "Strong"), (50, "Moderate"), (0, "Weak"))

CatalystStatus = Literal["detected", "none_detected", "unknown"]

FORMULA = {
    "positive_weights": POSITIVE_WEIGHTS,
    "penalty_weights": PENALTY_WEIGHTS,
    "raw": "sum(w_p*f_p for known positives) - sum(w_n*f_n for known penalties)",
    "normalization": "100 * (raw - worst) / (best - worst); best = sum of known positive weights, "
                     "worst = -sum of known penalty weights",
    "unknown_factors": "excluded and remaining weights renormalized (no reward, no penalty)",
    "rounding": f"nearest {SCORE_STEP} points",
    "bands": {name: f">= {lo}" for lo, name in BANDS},
    "factor_definitions": {
        "financial_strength": "mean of clamp(net_margin/0.20), clamp(fcf_margin/0.15), "
                              "clamp((revenue_growth+0.05)/0.25), net-cash term",
        "catalyst_materiality": "max over recent/upcoming events of materiality x link confidence "
                                "(x0.5 if negative impact, x0.5 if unconfirmed); unknown if news not checked",
        "valuation": "clamp((base_return_12m + 0.10) / 0.50); shrunk halfway toward 0.5 when the valuation "
                     "status is limited_data; unknown (excluded) unless the valuation is valid or limited",
        "data_freshness": "(1.0 if price current, 0.7 if <=2 sessions old, else 0.3) x "
                          "(1.0 if latest filing <=120 days old, else 0.7)",
        "forecast_uncertainty": "clamp((bull_return_12m - bear_return_12m) / 1.5)",
        "downside_risk": "clamp(-bear_return_12m / 0.6)",
    },
}


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def financial_strength(m: dict[str, Any]) -> float | None:
    parts = []
    if m.get("net_margin") is not None:
        parts.append(clamp01(m["net_margin"] / 0.20))
    if m.get("fcf_margin") is not None:
        parts.append(clamp01(m["fcf_margin"] / 0.15))
    if m.get("revenue_growth_yoy") is not None:
        parts.append(clamp01((m["revenue_growth_yoy"] + 0.05) / 0.25))
    nc, rev = m.get("net_cash"), m.get("revenue_ttm")
    if nc is not None and rev:
        parts.append(1.0 if nc >= 0 else clamp01(1 + nc / rev))  # net debt relative to revenue
    return sum(parts) / len(parts) if parts else None


def data_freshness(stale_sessions: int | None, filing_age_days: int | None) -> float | None:
    if stale_sessions is None and filing_age_days is None:
        return None
    price_part = 1.0 if not stale_sessions else 0.7 if stale_sessions <= 2 else 0.3
    filing_part = 1.0 if filing_age_days is not None and filing_age_days <= 120 else 0.7
    return price_part * filing_part


def factor_values(snapshot: dict[str, Any], catalyst_status: CatalystStatus, catalyst_score: float | None,
                  filing_age_days: int | None) -> dict[str, float | None]:
    base = snapshot.get("base_return_12m")
    bear = snapshot.get("bear_return_12m")
    bull = snapshot.get("bull_return_12m")
    if catalyst_status == "unknown":
        catalyst = None
    elif catalyst_status == "none_detected":
        catalyst = 0.0
    else:
        catalyst = clamp01(catalyst_score or 0.0)
    vstatus = snapshot.get("valuation_status")
    valuation = clamp01((base + 0.10) / 0.50) if base is not None else None
    if vstatus in ("requires_review", "unsupported_valuation", "invalid_inputs"):
        valuation = None  # an unsupported result can never look attractive
    elif valuation is not None and vstatus == "limited_data":
        valuation = 0.5 + (valuation - 0.5) * 0.5  # fallback evidence: less conviction either way
    return {
        "financial_strength": financial_strength(snapshot),
        "catalyst_materiality": catalyst,
        "valuation": valuation,
        "data_freshness": data_freshness(snapshot.get("stale_sessions", 0), filing_age_days),
        "forecast_uncertainty": clamp01((bull - bear) / 1.5) if bull is not None and bear is not None else None,
        "downside_risk": clamp01(-bear / 0.6) if bear is not None else None,
    }


@dataclass
class ScoreResult:
    score: float | None  # rounded to SCORE_STEP
    exact: float | None  # unrounded 0..100
    band: str
    factors: dict[str, float | None]
    contributions: dict[str, float]  # points on the 0..100 scale (penalties negative)
    unknown_factors: list[str] = field(default_factory=list)
    raw: float | None = None
    best: float | None = None
    worst: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score, "exact": self.exact, "band": self.band,
            "factors": {k: (round(v, 4) if v is not None else None) for k, v in self.factors.items()},
            "contributions": self.contributions, "unknown_factors": self.unknown_factors,
            "raw": self.raw, "best": self.best, "worst": self.worst, "weights": FACTOR_WEIGHTS,
        }


def band_for(score: float | None) -> str:
    if score is None:
        return "Unscored"
    return next(name for lo, name in BANDS if score >= lo)


def compute_score(factors: dict[str, float | None]) -> ScoreResult:
    known_pos = {k: w for k, w in POSITIVE_WEIGHTS.items() if factors.get(k) is not None}
    known_pen = {k: w for k, w in PENALTY_WEIGHTS.items() if factors.get(k) is not None}
    unknown = [k for k in FACTOR_WEIGHTS if factors.get(k) is None]
    best = sum(known_pos.values())
    worst = -sum(known_pen.values())
    span = best - worst
    if span <= 0:
        return ScoreResult(None, None, "Unscored", factors, {}, unknown)
    raw = sum(w * factors[k] for k, w in known_pos.items()) - sum(w * factors[k] for k, w in known_pen.items())
    exact = 100.0 * (raw - worst) / span
    contributions = {k: round(100.0 * w * factors[k] / span, 2) for k, w in known_pos.items()}
    contributions.update({k: round(-100.0 * w * factors[k] / span, 2) for k, w in known_pen.items()})
    rounded = float(round(exact / SCORE_STEP) * SCORE_STEP)
    return ScoreResult(rounded, round(exact, 3), band_for(rounded), factors, contributions, unknown,
                       round(raw, 6), round(best, 6), round(worst, 6))
