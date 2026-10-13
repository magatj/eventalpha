"""Quantitative forecasting engine.

Price targets are computed here — never by the language model. The AI layer may only
supply small, bounded, evidence-backed adjustments to growth/margin assumptions.

Outputs are *fundamental valuation estimates at a horizon*, not predictions that the
market price will reach a value on a date. Short-term price movement is described
separately with a historical-volatility range.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal

from dateutil.relativedelta import relativedelta

from app.services.forecasting.sectors import SectorProfile

MODEL_VERSION = "quant-1.0.0"
HORIZONS_MONTHS = (3, 6, 12, 24, 36, 60)
Scenario = Literal["bear", "base", "bull"]
SCENARIOS: tuple[Scenario, ...] = ("bear", "base", "bull")

LONG_RUN_GROWTH = 0.04  # nominal long-run revenue growth that high/low growth fades toward
GROWTH_FADE = 0.75  # per-year persistence of excess growth
GROWTH_CLAMP = (-0.30, 0.60)
MAX_AI_GROWTH_ADJ = 0.05
MAX_AI_MARGIN_ADJ = 0.03
DCF_YEARS = 5
METHOD_WEIGHTS = {"forward_pe": 0.6, "dcf": 0.4}
MIN_EARNINGS_MARGIN = 0.02  # below this net margin, earnings multiples are not meaningful
WACC_BOUNDS = (0.075, 0.16)
MIN_PE, MAX_PE = 5.0, 80.0
MIN_PLAUSIBLE_RATIO, MAX_PLAUSIBLE_RATIO = 0.05, 10.0  # base target / price outside this => data error  # bounds applied to company-specific historical multiples
MAX_COMPANY_PE_MEDIAN = 100.0  # above this, history reflects depressed earnings, not valuation


@dataclass
class AIAdjustment:
    scenario: Scenario
    revenue_growth_pp: float = 0.0  # e.g. 0.02 = +2 percentage points of annual growth
    net_margin_pp: float = 0.0
    rationale: str = ""
    source_labels: list[str] = field(default_factory=list)


@dataclass
class ForecastInputs:
    ticker: str
    price: float
    price_date: date
    sector: SectorProfile
    revenue_ttm: float | None
    revenue_growth_yoy: float | None
    revenue_cagr_3y: float | None
    net_margin: float | None
    operating_margin: float | None
    fcf_margin: float | None
    eps_ttm: float | None
    shares: float | None
    share_change_rate: float | None
    cash: float | None
    debt: float | None
    margin_history: list[dict[str, Any]] = field(default_factory=list)
    beta: float | None = None
    volatility: float | None = None
    historical_pe_band: tuple[float, float, float] | None = None  # p25, p50, p75
    currency_ok: bool = True
    risk_free_rate: float = 0.0425
    equity_risk_premium: float = 0.05
    pre_tax_cost_of_debt: float = 0.055
    tax_rate: float = 0.21
    ai_adjustments: list[AIAdjustment] = field(default_factory=list)


@dataclass
class Assumption:
    name: str
    value: Any
    basis: str
    kind: Literal["observed", "derived", "model_assumption", "ai_adjustment"] = "model_assumption"


@dataclass
class ScenarioResult:
    scenario: Scenario
    revenue: float | None
    revenue_growth_annual: float | None
    operating_margin: float | None
    net_margin: float | None
    eps: float | None
    free_cash_flow: float | None
    pe_multiple: float | None
    target_price_pe: float | None
    target_price_dcf: float | None
    target_price_ev_sales: float | None
    target_price: float | None
    potential_return: float | None
    explanation: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class HorizonForecast:
    horizon_months: int
    target_date: date
    observed_price: float
    observed_price_date: date
    scenarios: dict[str, ScenarioResult]
    methods: dict[str, Any]
    assumptions: list[Assumption]
    sensitivity: dict[str, Any]
    volatility_range: dict[str, Any] | None
    warnings: list[str]
    model_version: str = MODEL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------------
# building blocks (pure, unit-tested)
# ---------------------------------------------------------------------------------
def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def projected_revenue(current: float, growth: float, years: float) -> float:
    """Projected Revenue = Current Revenue × (1 + g)^years (constant growth)."""
    return current * (1 + growth) ** years


def faded_growth_path(g0: float, years: int, long_run: float = LONG_RUN_GROWTH, fade: float = GROWTH_FADE) -> list[float]:
    """Annual growth rates that decay geometrically from g0 toward the long-run rate."""
    return [long_run + (g0 - long_run) * fade**k for k in range(years)]


def project_revenue_faded(current: float, g0: float, years: float) -> float:
    """Revenue after `years` (fractional allowed) along the faded growth path."""
    rev = current
    full = int(math.floor(years))
    path = faded_growth_path(g0, full + 1)
    for k in range(full):
        rev *= 1 + path[k]
    frac = years - full
    if frac > 0:
        rev *= (1 + path[full]) ** frac
    return rev


def eps_from(revenue: float, net_margin: float, shares: float) -> float:
    return revenue * net_margin / shares


def forward_pe_target(eps: float | None, pe: float | None) -> float | None:
    """Target Price = Forecast EPS × Estimated P/E (None when EPS is not positive)."""
    if eps is None or pe is None or eps <= 0:
        return None
    return eps * pe


def cost_of_equity(rf: float, beta: float, erp: float) -> float:
    return rf + beta * erp


def wacc(equity_value: float, debt: float, ke: float, kd_pre_tax: float, tax: float) -> float:
    total = equity_value + max(debt, 0.0)
    if total <= 0:
        return ke
    return equity_value / total * ke + max(debt, 0.0) / total * kd_pre_tax * (1 - tax)


@dataclass
class DCFResult:
    enterprise_value: float
    equity_value: float
    per_share: float
    pv_fcf: float
    pv_terminal: float
    terminal_value: float
    discount_rate: float
    terminal_growth: float
    fcfs: list[float]

    @property
    def terminal_share(self) -> float:
        return self.pv_terminal / self.enterprise_value if self.enterprise_value else float("nan")


def dcf_valuation(fcfs: list[float], discount_rate: float, terminal_growth: float, cash: float, debt: float,
                  shares: float) -> DCFResult:
    """Classic Gordon-growth DCF on explicit free cash flows."""
    if discount_rate <= terminal_growth:
        raise ValueError("discount rate must exceed terminal growth")
    if shares <= 0:
        raise ValueError("shares must be positive")
    pv = sum(f / (1 + discount_rate) ** (i + 1) for i, f in enumerate(fcfs))
    tv = fcfs[-1] * (1 + terminal_growth) / (discount_rate - terminal_growth)
    pv_tv = tv / (1 + discount_rate) ** len(fcfs)
    ev = pv + pv_tv
    equity = ev + cash - debt
    return DCFResult(ev, equity, equity / shares, pv, pv_tv, tv, discount_rate, terminal_growth, fcfs)


# ---------------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------------
class ForecastEngine:
    def __init__(self, inp: ForecastInputs):
        self.i = inp
        self.warnings: list[str] = []
        self.assumptions: list[Assumption] = []
        self._prepare()

    # -- derived base assumptions ----------------------------------------------------
    def _prepare(self) -> None:
        i = self.i
        self.ai = {a.scenario: a for a in i.ai_adjustments}

        g_parts = [g for g in (i.revenue_growth_yoy, i.revenue_cagr_3y) if g is not None]
        self.g_hist = clamp(sum(g_parts) / len(g_parts), *GROWTH_CLAMP) if g_parts else LONG_RUN_GROWTH
        if not g_parts:
            self.warnings.append("No revenue growth history; base growth set to the long-run default.")
        hist_growth = []
        revs = [h["revenue"] for h in i.margin_history if h.get("revenue")]
        for a, b in zip(revs, revs[1:], strict=False):
            if a > 0:
                hist_growth.append(b / a - 1)
        g_std = statistics.pstdev(hist_growth) if len(hist_growth) >= 3 else None
        self.g_spread = clamp(max(0.04, g_std or 0.0, 0.25 * abs(self.g_hist)), 0.04, 0.20)

        self.m_ttm = i.net_margin
        hist_m = [h["net_margin"] for h in i.margin_history if h.get("net_margin") is not None]
        self.hist_margins = hist_m
        self.share_rate = clamp(i.share_change_rate, -0.05, 0.05) if i.share_change_rate is not None else 0.0
        self.beta = clamp(i.beta, 0.6, 2.0) if i.beta is not None else 1.0
        self.ke = cost_of_equity(i.risk_free_rate, self.beta, i.equity_risk_premium)
        mcap = i.price * (i.shares or 0)
        self.wacc = clamp(wacc(mcap, i.debt or 0.0, self.ke, i.pre_tax_cost_of_debt, i.tax_rate), *WACC_BOUNDS)

        self.current_pe = (i.price / i.eps_ttm) if i.eps_ttm and i.eps_ttm > 0 else None
        s = i.sector
        band = i.historical_pe_band
        if band and band[1] > MAX_COMPANY_PE_MEDIAN:
            self.warnings.append(
                f"Company's historical P/E (median {band[1]:.0f}x) is distorted by depressed earnings; "
                "using the sector band only.")
            band = None
        if band:
            p25, p50, p75 = (clamp(x, MIN_PE, MAX_PE) for x in band)
            self.pe_band = (0.5 * p25 + 0.5 * s.pe_low, 0.5 * p50 + 0.5 * s.pe_mid, 0.5 * p75 + 0.5 * s.pe_high)
            pe_basis = (f"50/50 blend of company 2-year trailing P/E band ({p25:.1f}/{p50:.1f}/{p75:.1f}, "
                        f"clamped to {MIN_PE:.0f}–{MAX_PE:.0f}x) and {s.name} sector band "
                        f"({s.pe_low}/{s.pe_mid}/{s.pe_high})")
        else:
            self.pe_band = (s.pe_low, s.pe_mid, s.pe_high)
            pe_basis = f"{s.name} sector reference band (model assumption)"

        self.assumptions += [
            Assumption("Observed price", round(i.price, 4), f"close on {i.price_date}", "observed"),
            Assumption("Base revenue growth (start)", round(self.g_hist, 4),
                       "average of TTM YoY growth and 3-year CAGR, clamped to [-30%, +60%]", "derived"),
            Assumption("Scenario growth spread", round(self.g_spread, 4),
                       "max(4pp, stdev of annual growth, 25% of base growth), capped at 20pp", "model_assumption"),
            Assumption("Growth fade", f"{GROWTH_FADE:.2f}/yr toward {LONG_RUN_GROWTH:.0%}",
                       "excess growth decays geometrically toward long-run nominal growth", "model_assumption"),
            Assumption("Share count change / yr", round(self.share_rate, 4),
                       "2-year diluted share CAGR, clamped to ±5% (buybacks negative)", "derived"),
            Assumption("P/E band (bear/base/bull)", [round(x, 1) for x in self.pe_band], pe_basis, "model_assumption"),
            Assumption("Beta", round(self.beta, 2),
                       "weekly returns vs SPY over 2 years, clamped to [0.6, 2.0]" if i.beta is not None
                       else "default 1.0 (insufficient price history)", "derived" if i.beta is not None else "model_assumption"),
            Assumption("Cost of equity", round(self.ke, 4),
                       f"CAPM: rf {i.risk_free_rate:.2%} + beta × ERP {i.equity_risk_premium:.2%}", "model_assumption"),
            Assumption("Discount rate (WACC)", round(self.wacc, 4),
                       f"market-value weights; pre-tax cost of debt {i.pre_tax_cost_of_debt:.2%}, tax {i.tax_rate:.0%}; "
                       f"bounded to {WACC_BOUNDS[0]:.1%}–{WACC_BOUNDS[1]:.0%}", "model_assumption"),
        ]
        for a in i.ai_adjustments:
            self.assumptions.append(Assumption(
                f"AI-suggested adjustment ({a.scenario})",
                {"revenue_growth_pp": a.revenue_growth_pp, "net_margin_pp": a.net_margin_pp},
                f"{a.rationale} [sources: {', '.join(a.source_labels) or 'none'}]", "ai_adjustment"))

        self.method_notes: dict[str, str] = {}
        # One valuation regime per company (not per scenario), so scenarios stay comparable:
        # earnings multiples only when the business is meaningfully profitable today.
        profitable = (self.m_ttm or 0) >= MIN_EARNINGS_MARGIN and (i.eps_ttm or 0) > 0
        self.regime = "earnings" if profitable else "sales"
        self.pe_ok = s.pe_applicable and i.currency_ok and profitable
        if not profitable and s.pe_applicable:
            self.method_notes["forward_pe"] = (
                f"Net margin below {MIN_EARNINGS_MARGIN:.0%} or EPS not positive; earnings multiples are not meaningful, "
                "so EV/Sales is used for all scenarios.")
        self.dcf_ok = s.dcf_applicable and i.currency_ok and (i.fcf_margin or 0) > 0 and bool(i.shares)
        if not i.currency_ok:
            self.method_notes["all"] = ("Per-share financials are not comparable with the USD share price "
                                        "(non-USD reporting or ADR of a foreign issuer); per-share valuation disabled.")
        if not s.dcf_applicable:
            self.method_notes["dcf"] = s.note or "DCF not applicable to this sector."
        elif (i.fcf_margin or 0) <= 0:
            self.method_notes["dcf"] = "Trailing free cash flow is negative or unavailable; DCF not applied."
        if not s.pe_applicable:
            self.method_notes["forward_pe"] = s.note or "P/E not applicable to this sector."
        self.fcf_unstable = False
        fcf_hist = [h["fcf_margin"] for h in i.margin_history if h.get("fcf_margin") is not None]
        if self.dcf_ok and fcf_hist and (min(fcf_hist) < 0 or (len(fcf_hist) >= 3 and statistics.pstdev(fcf_hist) > 0.5 * abs(statistics.mean(fcf_hist)))):
            self.fcf_unstable = True
            self.method_notes["dcf"] = "Historical free cash flow is volatile; DCF weight reduced to 25%."

    # -- scenario parameters ----------------------------------------------------------
    def scenario_growth(self, sc: Scenario) -> float:
        g = self.g_hist + {"bear": -self.g_spread, "base": 0.0, "bull": self.g_spread}[sc]
        adj = self.ai.get(sc)
        if adj:
            g += clamp(adj.revenue_growth_pp, -MAX_AI_GROWTH_ADJ, MAX_AI_GROWTH_ADJ)
        return clamp(g, *GROWTH_CLAMP)

    def scenario_margin(self, sc: Scenario) -> float | None:
        m = self.m_ttm
        if m is None:
            return None
        hist = self.hist_margins
        if sc == "base":
            target = 0.5 * m + 0.5 * statistics.median(hist) if len(hist) >= 3 else m
        elif sc == "bear":
            target = max(min(m - 0.25 * abs(m), min(hist) if hist else m), m - 0.10)
        else:
            target = min(max(m + 0.15 * abs(m), max(hist) if hist else m), m + 0.08)
        adj = self.ai.get(sc)
        if adj:
            target += clamp(adj.net_margin_pp, -MAX_AI_MARGIN_ADJ, MAX_AI_MARGIN_ADJ)
        return clamp(target, -1.0, 0.6)

    def scenario_pe(self, sc: Scenario, years: float) -> float:
        lo, mid, hi = self.pe_band
        if sc == "bear":
            return lo
        if sc == "bull":
            return hi
        if self.current_pe is None:
            return mid
        # Start from today's multiple (bounded by the band) and revert toward the midpoint over time,
        # so base never exceeds the bull multiple nor falls below the bear multiple.
        cur = clamp(self.current_pe, lo, hi)
        return mid + (cur - mid) * 0.5 ** years

    def scenario_fcf_margin(self, sc: Scenario) -> float | None:
        f = self.i.fcf_margin
        if f is None:
            return None
        hist = [h["fcf_margin"] for h in self.i.margin_history if h.get("fcf_margin") is not None]
        base = 0.5 * f + 0.5 * statistics.median(hist) if len(hist) >= 3 else f
        return {"bear": base - 0.25 * abs(base), "base": base, "bull": base + 0.15 * abs(base)}[sc]

    def terminal_growth(self, sc: Scenario) -> float:
        g = {"bear": 0.02, "base": 0.025, "bull": 0.03}[sc]
        return min(g, self.wacc - 0.03)

    # -- core --------------------------------------------------------------------------
    def _scenario(self, sc: Scenario, years: float) -> ScenarioResult:
        i = self.i
        rev0 = i.revenue_ttm
        g = self.scenario_growth(sc)
        if not rev0 or rev0 <= 0 or not i.shares:
            return ScenarioResult(sc, None, g, None, None, None, None, None, None, None, None, None, None,
                                  "Insufficient revenue or share data to build this scenario.")
        rev_t = project_revenue_faded(rev0, g, years)
        cagr = (rev_t / rev0) ** (1 / years) - 1 if years > 0 else g
        m_s = self.scenario_margin(sc)
        m_t = None if m_s is None or self.m_ttm is None else self.m_ttm + (m_s - self.m_ttm) * min(1.0, years)
        op_m = None
        if i.operating_margin is not None and m_t is not None and self.m_ttm is not None:
            op_m = i.operating_margin + (m_t - self.m_ttm)
        shares_t = i.shares * (1 + self.share_rate) ** years
        eps_t = eps_from(rev_t, m_t, shares_t) if m_t is not None else None

        targets: dict[str, float | None] = {"forward_pe": None, "dcf": None, "ev_sales": None}
        details: dict[str, Any] = {"shares": shares_t, "years": years, "growth_start": g}

        pe = self.scenario_pe(sc, years) if self.pe_ok else None
        if self.pe_ok:
            targets["forward_pe"] = forward_pe_target(eps_t, pe)
            if targets["forward_pe"] is None:
                details["forward_pe_note"] = "Projected EPS not positive; P/E method not applicable."

        fcf_t = None
        fcf_m = self.scenario_fcf_margin(sc)
        if self.dcf_ok and fcf_m is not None and fcf_m > 0:
            fcf_t = rev_t * fcf_m
            path = faded_growth_path(g, int(math.ceil(years)) + DCF_YEARS)
            start = int(math.ceil(years))
            r = rev_t
            fcfs = []
            for k in range(DCF_YEARS):
                r *= 1 + path[start + k]
                fcfs.append(r * fcf_m)
            tg = self.terminal_growth(sc)
            try:
                d = dcf_valuation(fcfs, self.wacc, tg, i.cash or 0.0, i.debt or 0.0, shares_t)
                targets["dcf"] = d.per_share if d.per_share > 0 else None
                details["dcf"] = {
                    "fcf_margin": fcf_m, "discount_rate": self.wacc, "terminal_growth": tg,
                    "enterprise_value": d.enterprise_value, "equity_value": d.equity_value,
                    "pv_terminal_share": d.terminal_share, "projected_fcfs": fcfs,
                }
                if d.terminal_share > 0.85:
                    details["dcf"]["note"] = "Terminal value exceeds 85% of EV; result is assumption-sensitive."
            except ValueError as exc:
                details["dcf_note"] = str(exc)

        if self.i.currency_ok and (self.regime == "sales" or targets["forward_pe"] is None):
            s = i.sector
            evs = {"bear": s.ev_sales_low, "base": s.ev_sales_mid, "bull": s.ev_sales_high}[sc]
            eq = rev_t * evs + (i.cash or 0.0) - (i.debt or 0.0)
            targets["ev_sales"] = eq / shares_t if eq > 0 else None
            details["ev_sales_multiple"] = evs

        weights: dict[str, float] = {}
        if targets["forward_pe"] is not None:
            weights["forward_pe"] = METHOD_WEIGHTS["forward_pe"]
        if targets["dcf"] is not None:
            weights["dcf"] = 0.25 if self.fcf_unstable else METHOD_WEIGHTS["dcf"]
        if targets["ev_sales"] is not None and "forward_pe" not in weights:
            weights["ev_sales"] = METHOD_WEIGHTS["forward_pe"] if "dcf" in weights else 1.0
        total_w = sum(weights.values())
        target = sum(targets[k] * w for k, w in weights.items()) / total_w if total_w else None  # type: ignore[operator]
        details["method_weights"] = {k: round(w / total_w, 3) for k, w in weights.items()} if total_w else {}

        ret = target / i.price - 1 if target is not None else None
        expl = self._explain(sc, cagr, m_t, eps_t, pe, details, weights)
        return ScenarioResult(
            scenario=sc, revenue=rev_t, revenue_growth_annual=cagr, operating_margin=op_m, net_margin=m_t,
            eps=eps_t, free_cash_flow=fcf_t, pe_multiple=pe, target_price_pe=targets["forward_pe"],
            target_price_dcf=targets["dcf"], target_price_ev_sales=targets["ev_sales"], target_price=target,
            potential_return=ret, explanation=expl, details=details,
        )

    def _explain(self, sc: str, cagr: float, m: float | None, eps: float | None, pe: float | None,
                 details: dict, weights: dict) -> str:
        parts = [f"{sc.title()}: revenue grows ~{cagr:.1%}/yr over the horizon"]
        if m is not None:
            parts.append(f"net margin {m:.1%}")
        if eps is not None:
            parts.append(f"EPS {eps:.2f}")
        if pe is not None and "forward_pe" in weights:
            parts.append(f"valued at {pe:.1f}x earnings")
        if "dcf" in weights and "dcf" in details:
            d = details["dcf"]
            parts.append(f"DCF at {d['discount_rate']:.1%} discount / {d['terminal_growth']:.1%} terminal growth")
        if "ev_sales" in weights:
            parts.append(f"EV/Sales {details.get('ev_sales_multiple')}x (earnings not positive)")
        if not weights:
            parts.append("no applicable valuation method")
        return "; ".join(parts) + "."

    def sensitivity(self, years: float) -> dict[str, Any]:
        i = self.i
        out: dict[str, Any] = {}
        if not i.revenue_ttm or not i.shares or self.m_ttm is None:
            return out
        base_g = self.scenario_growth("base")
        m_s = self.scenario_margin("base")
        m_t = self.m_ttm + ((m_s or self.m_ttm) - self.m_ttm) * min(1.0, years)
        shares_t = i.shares * (1 + self.share_rate) ** years
        base_pe = self.scenario_pe("base", years)
        g_axis = [round(base_g + d, 4) for d in (-0.04, -0.02, 0.0, 0.02, 0.04)]
        pe_axis = [round(base_pe * f, 2) for f in (0.8, 0.9, 1.0, 1.1, 1.2)]
        grid = []
        for g in g_axis:
            eps = eps_from(project_revenue_faded(i.revenue_ttm, g, years), m_t, shares_t)
            grid.append([round(eps * pe, 2) if eps > 0 else None for pe in pe_axis])
        out["pe_growth"] = {"rows_growth": g_axis, "cols_pe": pe_axis, "target_prices": grid,
                            "note": "Forward P/E target price for base-case margin"}
        if self.dcf_ok:
            fcf_m = self.scenario_fcf_margin("base")
            if fcf_m and fcf_m > 0:
                w_axis = [round(self.wacc + d, 4) for d in (-0.01, -0.005, 0.0, 0.005, 0.01)]
                t_axis = [0.015, 0.02, 0.025, 0.03, 0.035]
                rev_t = project_revenue_faded(i.revenue_ttm, base_g, years)
                path = faded_growth_path(base_g, int(math.ceil(years)) + DCF_YEARS)
                r, fcfs = rev_t, []
                for k in range(DCF_YEARS):
                    r *= 1 + path[int(math.ceil(years)) + k]
                    fcfs.append(r * fcf_m)
                dgrid = []
                for w in w_axis:
                    row = []
                    for t in t_axis:
                        if w - t < 0.02:
                            row.append(None)
                            continue
                        d = dcf_valuation(fcfs, w, t, i.cash or 0.0, i.debt or 0.0, shares_t)
                        row.append(round(d.per_share, 2) if d.per_share > 0 else None)
                    dgrid.append(row)
                out["dcf_wacc_terminal"] = {"rows_wacc": w_axis, "cols_terminal_growth": t_axis,
                                            "per_share_values": dgrid}
        return out

    def run(self, horizon_months: int) -> HorizonForecast:
        i = self.i
        years = horizon_months / 12
        scenarios = {sc: self._scenario(sc, years) for sc in SCENARIOS}
        warnings = list(self.warnings)
        base_t = scenarios["base"].target_price
        if base_t is not None and not (MIN_PLAUSIBLE_RATIO <= base_t / i.price <= MAX_PLAUSIBLE_RATIO):
            # A fundamental value 10x above (or 95% below) the market price almost always means bad
            # input data (units, share counts), not an opportunity. Withhold rather than mislead.
            warnings.append(
                f"Implausible valuation (base target {base_t / i.price:,.1f}× the current price) — likely a "
                "data error in the source filings; price targets withheld.")
            for s in scenarios.values():
                s.details["withheld_target"] = s.target_price
                s.target_price = s.target_price_pe = s.target_price_dcf = s.target_price_ev_sales = None
                s.potential_return = None
        t = [scenarios[s].target_price for s in SCENARIOS]
        if all(x is not None for x in t) and not (t[0] <= t[1] <= t[2]):  # type: ignore[operator]
            warnings.append("Scenario targets are not monotonic (bear ≤ base ≤ bull); methods disagree.")
        vol_range = None
        if i.volatility:
            s = i.volatility * math.sqrt(years)
            vol_range = {
                "sigma_annual": i.volatility,
                "low_1sigma": i.price * math.exp(-s), "high_1sigma": i.price * math.exp(s),
                "note": ("Historical-volatility 1σ band (≈68% of outcomes if log-returns were normal with zero "
                         "drift). Describes typical short-term price movement; it is not a forecast."),
            }
        methods = {
            "forward_pe": {"applicable": self.pe_ok, "note": self.method_notes.get("forward_pe")},
            "dcf": {"applicable": self.dcf_ok, "note": self.method_notes.get("dcf"), "reduced_weight": self.fcf_unstable},
            "ev_sales": {"applicable": self.i.currency_ok,
                         "note": "Primary method in the sales regime (unprofitable/marginal companies); otherwise a "
                                 "fallback for scenarios whose projected EPS is not positive."},
            "regime": self.regime,
            "weights": METHOD_WEIGHTS, "general": self.method_notes.get("all"),
            "interpretation": ("Targets are fundamental value estimates at the horizon date under each scenario. "
                               "They are not predictions that the market price will reach these values by that date. "
                               "No probabilities are assigned to scenarios."),
        }
        return HorizonForecast(
            horizon_months=horizon_months, target_date=i.price_date + relativedelta(months=horizon_months),
            observed_price=i.price, observed_price_date=i.price_date, scenarios=scenarios, methods=methods,
            assumptions=list(self.assumptions), sensitivity=self.sensitivity(years), volatility_range=vol_range,
            warnings=warnings,
        )


def run_all_horizons(inp: ForecastInputs, horizons: tuple[int, ...] = HORIZONS_MONTHS) -> list[HorizonForecast]:
    eng = ForecastEngine(inp)
    return [eng.run(h) for h in horizons]
