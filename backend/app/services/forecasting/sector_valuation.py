"""Industry-specific valuation models (banks, insurers, REITs).

All inputs come from SEC filings; nothing is invented. When an input is unavailable the model is
not built (the registry then marks the valuation unsupported or limited).

Book-value model (banks: tangible book / ROTE, insurers: book / ROE)
    normalized return  r  = median of annual returns (>=3 years) — banks blend 50/50 with TTM
    retention          b  = 1 - payout (reported dividends / net income; 0.6 assumed when unknown)
    book per share     BV_t = BV_0 x (1 + r_s x b) ^ years            (r_s = scenario return)
    justified multiple M* = (r - g) / (k_e - g),  g = min(r x b, 4%), k_e >= 9%   (Gordon growth on book)
    target             BV_t x multiple   (multiple from company history / peers / M*, see company_multiples)

FFO model (REITs)
    FFO = net income to common + depreciation & amortization - gains on property sales
    FFO_t / share grows with the scenario revenue path; target = FFO_t x P/FFO multiple
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from app.services.forecasting.company_multiples import MultipleSelection

DEFAULT_RETENTION = 0.6
RETURN_BOUNDS = (-0.10, 0.35)
JUSTIFIED_BOUNDS = (0.5, 3.0)
FINANCIAL_KE_FLOOR = 0.09  # minimum cost of equity for book-value models (low-beta financials still carry
                           # leverage and credit/catastrophe risk that beta under-measures)
SUSTAINABLE_GROWTH_CAP = 0.04
PB_BOUNDS = (0.3, 6.0)
PFFO_BOUNDS = (5.0, 40.0)
REIT_PFFO_REFERENCE = (12.0, 16.0, 20.0)  # model assumption, used only without company history


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def normalized_return(history: list[float], ttm: float | None, blend_ttm: bool) -> tuple[float | None, str]:
    hist = [h for h in history if h is not None]
    if len(hist) >= 3:
        med = statistics.median(hist)
        if blend_ttm and ttm is not None:
            return clamp(0.5 * med + 0.5 * ttm, *RETURN_BOUNDS), f"50/50 blend of {len(hist)}-year median ({med:.1%}) and TTM ({ttm:.1%})"
        return clamp(med, *RETURN_BOUNDS), f"median of {len(hist)} annual values ({med:.1%})"
    if ttm is not None:
        return clamp(ttm, *RETURN_BOUNDS), f"TTM only ({ttm:.1%}; fewer than 3 years of history)"
    return None, "unavailable"


def justified_multiple(r: float, ke: float, retention: float) -> float | None:
    ke = max(ke, FINANCIAL_KE_FLOOR)
    g = max(0.0, min(r * retention, SUSTAINABLE_GROWTH_CAP))
    if ke - g <= 0.01:
        return None
    return clamp((r - g) / (ke - g), *JUSTIFIED_BOUNDS)


@dataclass
class BookValueModel:
    kind: str  # bank | insurance
    measure: str  # "tangible book" | "book"
    bvps0: float
    r_norm: float
    r_basis: str
    retention: float
    retention_basis: str
    ke: float
    justified: float | None
    selection: MultipleSelection
    notes: list[str] = field(default_factory=list)

    def scenario_return(self, sc: str) -> float:
        r = self.r_norm
        return {"bear": r - max(0.03, 0.25 * abs(r)), "base": r, "bull": r + min(0.02, 0.15 * abs(r))}[sc]

    def target(self, sc: str, years: float) -> tuple[float | None, dict[str, Any]]:
        r = self.scenario_return(sc)
        bv_t = self.bvps0 * (1 + r * self.retention) ** years
        m = {"bear": self.selection.bear, "base": self.selection.base_at(years), "bull": self.selection.bull}[sc]
        price = bv_t * m if bv_t > 0 else None
        return price, {"model": f"{self.measure} x multiple", "return_on_equity": r, "retention": self.retention,
                       f"{self.measure}_per_share": bv_t, "multiple": m}

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "measure": self.measure, "per_share_today": self.bvps0,
                "normalized_return": self.r_norm, "return_basis": self.r_basis, "retention": self.retention,
                "retention_basis": self.retention_basis, "cost_of_equity": self.ke,
                "justified_multiple": self.justified, "multiples": self.selection.to_dict(), "notes": self.notes}


@dataclass
class FFOModel:
    ffo_ps0: float
    ffo_basis: str
    selection: MultipleSelection
    notes: list[str] = field(default_factory=list)

    def target(self, sc: str, years: float, revenue_ratio: float | None) -> tuple[float | None, dict[str, Any]]:
        growth = revenue_ratio if revenue_ratio and revenue_ratio > 0 else 1.0
        adj = {"bear": 0.9, "base": 1.0, "bull": 1.05}[sc]  # bear: FFO margin -10%, bull: +5%
        ffo_t = self.ffo_ps0 * growth * adj
        m = {"bear": self.selection.bear, "base": self.selection.base_at(years), "bull": self.selection.bull}[sc]
        return (ffo_t * m if ffo_t > 0 else None), {"model": "FFO x P/FFO", "ffo_per_share": ffo_t, "multiple": m}

    def describe(self) -> dict[str, Any]:
        return {"ffo_per_share_today": self.ffo_ps0, "ffo_basis": self.ffo_basis, "multiples": self.selection.to_dict(),
                "notes": self.notes}


def ffo_per_share(ttm: dict[str, Any]) -> tuple[float | None, str]:
    """FFO = net income to common + D&A - property-sale gains, per diluted share (None if inputs missing)."""
    ni, da, sh = ttm.get("net_income_to_common"), ttm.get("depreciation"), ttm.get("diluted_shares")
    if ni is None or da is None or not sh:
        return None, "unavailable (net income, depreciation or shares not reported)"
    gains = ttm.get("gain_on_property_sales")
    ffo = ni + abs(da) - (gains or 0.0)
    basis = "net income + D&A - property-sale gains" if gains is not None else "net income + D&A (no property-sale gains tagged)"
    return ffo / sh, basis
