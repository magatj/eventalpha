"""Valuation-method registry: pick the method that fits the company's business model *and* data.

Defaults by business model (overridden when required inputs are missing):

    operating company / technology / industrial   forward P/E + DCF (FCF after stock comp)
    cyclical (energy, materials, shipping) and airlines
                                                   normalized earnings (P/E on mid-cycle margins) + DCF
                                                   (EV/EBITDAR: aircraft rent is not tagged consistently
                                                   in XBRL, so it is reported as unsupported)
    utilities                                      DCF + P/E (DCF dropped when FCF is negative)
    banks                                          P/TBV justified by sustainable ROTE + P/E
    insurers                                       P/B justified by normalized ROE + P/E
    other financials (brokers, lenders, payments)  P/E (FCF DCF not meaningful)
    REITs                                          P/FFO (FFO = net income + D&A - property-sale gains)
    unprofitable growth                            revenue multiple with an explicit margin path

A method is only activated when its inputs exist; otherwise the plan says which inputs are missing
and the analysis is marked limited or unsupported — an unsuitable generic method is never applied
silently.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PROFITABLE_HISTORY_MARGIN = 0.03


@dataclass
class ValuationPlan:
    business_model: str
    method: str
    label: str
    rationale: str
    weights: dict[str, float]
    required: dict[str, bool]
    status_hint: str = "valid"  # valid | limited | unsupported
    notes: list[str] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        return [k for k, ok in self.required.items() if not ok]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "missing": self.missing}


def _sic(sic: str | int | None) -> int | None:
    try:
        return int(sic) if sic is not None else None
    except (TypeError, ValueError):
        return None


def business_model(sic: str | int | None, sector: str | None, description: str | None = None) -> str:
    s = _sic(sic)
    desc = (description or "").lower()
    if s is not None:
        if 6020 <= s <= 6036:
            return "bank"
        if (6311 <= s <= 6399 or s == 6411) and sector == "Financials":
            return "insurance"
        if s == 6798:
            return "reit"
        if s in (4512, 4513, 4522):
            return "airline"
        if s in (4412, 4424) or "shipping" in desc or "deep sea" in desc:
            return "cyclical"
    if sector == "Utilities":
        return "utility"
    if sector == "Real Estate":
        return "real_estate"
    if sector == "Financials":
        return "other_financial"
    if sector in ("Energy", "Materials"):
        return "cyclical"
    if sector == "Technology":
        return "technology"
    return "operating_company"


def choose_plan(*, sic: str | int | None, sector: str | None, description: str | None, net_margin: float | None,
                eps: float | None, margin_median: float | None, margin_n: int, revenue_growth: float | None,
                cyclical: bool | None, currency_ok: bool, has_revenue: bool, has_shares: bool,
                book_value: float | None, tangible_book: float | None, goodwill_reported: bool,
                ffo_available: bool, ffo_gains_reported: bool, fcf_positive: bool) -> ValuationPlan:
    bm = business_model(sic, sector, description)
    profitable_now = (net_margin or 0) >= 0.02 and (eps or 0) > 0
    profitable_history = margin_median is not None and margin_n >= 3 and margin_median >= PROFITABLE_HISTORY_MARGIN
    base_req = {"comparable currency (USD filer)": currency_ok, "revenue": has_revenue, "share count": has_shares}

    def plan(method: str, label: str, why: str, weights: dict[str, float], extra: dict[str, bool] | None = None,
             notes: list[str] | None = None) -> ValuationPlan:
        req = {**base_req, **(extra or {})}
        hint = "unsupported" if not all(req.values()) else "valid"
        return ValuationPlan(bm, method, label, why, weights, req, hint, notes or [])

    if bm == "bank":
        return plan("bank_ptbv_roe", "P/TBV justified by sustainable ROTE (+ P/E)",
                    "Banks' debt and deposits are operating capital, so FCF-based DCF is not meaningful; banks are "
                    "valued on tangible book value and the return earned on it.",
                    {"book_value_model": 0.7, "forward_pe": 0.3},
                    {"book value": (book_value or 0) > 0, "net income": eps is not None},
                    [] if goodwill_reported else ["Goodwill/intangibles not tagged: tangible book = book value."])
    if bm == "insurance":
        return plan("insurance_pb_roe", "P/B justified by normalized ROE (+ P/E)",
                    "Insurers' earnings swing with catastrophe losses and investment gains; book value and a "
                    "multi-year ROE are steadier anchors than one year's EPS.",
                    {"book_value_model": 0.7, "forward_pe": 0.3},
                    {"book value": (book_value or 0) > 0, "net income": eps is not None})
    if bm in ("reit", "real_estate"):
        notes = [] if ffo_gains_reported else ["Property-sale gains not tagged: FFO = net income + D&A (gains assumed 0)."]
        return plan("reit_pffo", "P/FFO", "REIT earnings are depressed by real-estate depreciation; funds from "
                    "operations (FFO) is the standard earnings measure.", {"ffo_model": 1.0},
                    {"FFO inputs (net income + depreciation)": ffo_available}, notes)
    if not profitable_now and not profitable_history:
        if (revenue_growth or 0) >= 0.10:
            return plan("revenue_margin_path", "Revenue multiple with explicit margin path",
                        "Not yet profitable: earnings multiples are not meaningful; value is based on revenue with "
                        "an explicit path toward profitability.", {"ev_sales": 1.0})
        return plan("revenue_multiple", "Revenue multiple (unprofitable, low growth)",
                    "No positive earnings now or historically; a revenue multiple is the only applicable method.",
                    {"ev_sales": 1.0}, notes=["Low-growth unprofitable company: valuation reliability is low."])
    if bm == "other_financial":
        return plan("financial_pe", "P/E (financial company)",
                    "Financing activities are operating activities for lenders, brokers and payment firms, so "
                    "FCF-based DCF is not applied.", {"forward_pe": 1.0}, {"net income": eps is not None})
    if bm == "utility":
        w = {"forward_pe": 0.5, "dcf": 0.5} if fcf_positive else {"forward_pe": 1.0}
        return plan("utility_dcf_pe", "DCF + P/E (utility)", "Regulated, capital-intensive cash flows suit DCF; "
                    "P/E cross-checks it. DCF is dropped when heavy capex makes free cash flow negative.", w,
                    notes=[] if fcf_positive else ["Free cash flow negative (capex program): DCF not applied."])
    normalized = (not profitable_now) or bool(cyclical) or bm in ("cyclical", "airline")
    if bm == "airline":
        return plan("airline_normalized", "Normalized earnings + DCF (airline)",
                    "Airline earnings are cyclical and capital-intensive (aircraft); valued on mid-cycle margins with "
                    "full aircraft capex in free cash flow.", {"forward_pe": 0.6, "dcf": 0.4},
                    notes=["EV/EBITDAR not supported: aircraft rent is not tagged consistently in SEC XBRL."])
    if normalized:
        return plan("normalized_earnings", "Normalized earnings + DCF",
                    "Earnings are cyclical or temporarily away from the company's norm; valued on mid-cycle "
                    "margins rather than the latest year.", {"forward_pe": 0.6, "dcf": 0.4})
    label = "Forward P/E + DCF" + (" (technology)" if bm == "technology" else "")
    return plan("earnings_dcf", label, "Profitable operating company: forward earnings multiple cross-checked by "
                "a discounted cash flow (free cash flow after stock-based compensation).", {"forward_pe": 0.6, "dcf": 0.4})
