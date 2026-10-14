"""Assemble the evidence-based valuation inputs for one company (shared by live analysis and the
point-in-time replay) and the audit record that explains the resulting forecast."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Security
from app.services.financials import FinancialSnapshot, margin_stats
from app.services.forecasting.company_multiples import (
    MultipleBand,
    MultipleSelection,
    earnings_state,
    historical_multiples,
    select_multiples,
)
from app.services.forecasting.engine import AIAdjustment, ForecastInputs, HorizonForecast, cost_of_equity
from app.services.forecasting.peer_comparison import PeerSet, find_peers
from app.services.forecasting.sector_valuation import (
    DEFAULT_RETENTION,
    PB_BOUNDS,
    PFFO_BOUNDS,
    REIT_PFFO_REFERENCE,
    BookValueModel,
    FFOModel,
    ffo_per_share,
    justified_multiple,
    normalized_return,
)
from app.services.forecasting.sectors import SectorProfile
from app.services.forecasting.valuation_guardrails import GuardrailResult, evaluate
from app.services.forecasting.valuation_registry import ValuationPlan, choose_plan

PS_BOUNDS = (0.1, 30.0)


@dataclass
class ValuationContext:
    plan: ValuationPlan
    history: dict[str, MultipleBand]
    peers: PeerSet | None
    pe_selection: MultipleSelection | None
    ps_selection: MultipleSelection | None
    book_model: BookValueModel | None
    ffo_model: FFOModel | None
    market_cap: float | None
    market_cap_basis: str
    shares_current: float | None
    share_basis_ok: bool
    notes: list[str] = field(default_factory=list)

    def primary_kind(self) -> str:
        if self.book_model is not None and "book_value_model" in self.plan.weights:
            return "book"
        if self.ffo_model is not None and "ffo_model" in self.plan.weights:
            return "ffo"
        if "ev_sales" in self.plan.weights:
            return "ps"
        return "pe"


def build_inputs(session: Session | None, *, ticker: str, mode: str, sector_name: str | None, sector: SectorProfile,
                 sic: str | None, industry: str | None, snap: FinancialSnapshot, facts: list,
                 prices: pd.DataFrame, price: float, price_date: date, beta: float | None, volatility: float | None,
                 splits: list[tuple[date, float]], settings, provider_shares: float | None = None,
                 ai_adjustments: list[AIAdjustment] | None = None, use_peers: bool = True
                 ) -> tuple[ForecastInputs, ValuationContext]:
    t, r = snap.ttm, snap.ratios
    ms = margin_stats(snap.margin_history)
    state = earnings_state(r.get("net_margin"), ms["median"], [h.get("net_margin") for h in snap.margin_history])
    currency_ok = snap.currency == "USD" and not snap.foreign_issuer
    ffo_ps, ffo_basis = ffo_per_share(t)
    plan = choose_plan(
        sic=sic, sector=sector_name, description=industry, net_margin=r.get("net_margin"), eps=t.get("eps"),
        margin_median=ms["median"], margin_n=ms["n"], revenue_growth=r.get("revenue_growth_yoy"),
        cyclical=ms.get("cyclical"), currency_ok=currency_ok, has_revenue=bool(t.get("revenue")),
        has_shares=bool(t.get("diluted_shares") or t.get("shares_outstanding")), book_value=t.get("book_value"),
        tangible_book=t.get("tangible_book_value"),
        goodwill_reported=bool((snap.provenance.get("tangible_book_value") or {}).get("goodwill_reported")),
        ffo_available=ffo_ps is not None, ffo_gains_reported=t.get("gain_on_property_sales") is not None,
        fcf_positive=(r.get("fcf_after_sbc_margin") if r.get("fcf_after_sbc_margin") is not None else r.get("fcf_margin") or 0) > 0,
    )
    is_reit = plan.method == "reit_pffo"
    history = historical_multiples(facts, prices, splits, ffo_fn=(lambda s: ffo_per_share(s.ttm)[0]) if is_reit else None)
    notes: list[str] = []

    # current shares outstanding (market cap) — SEC cover page, split-adjusted; else the market-data provider
    shares_current, basis = t.get("shares_outstanding"), "price x SEC shares outstanding"
    if shares_current is None and provider_shares:
        shares_current, basis = provider_shares, "price x provider shares outstanding (Massive)"
    mcap = price * shares_current if shares_current else None
    share_basis_ok = not any("differ by more than 2x" in v for v in snap.validation)
    if shares_current and provider_shares and not 0.5 <= provider_shares / shares_current <= 2.0:
        notes.append(f"SEC shares outstanding ({shares_current:,.0f}) and provider shares ({provider_shares:,.0f}) "
                     "differ by more than 2x (possible multi-class or split timing); market cap uses SEC.")

    peers = None
    if use_peers and session is not None:
        sec = session.scalar(select(Security).where(Security.data_mode == mode, Security.ticker == ticker))
        peers = find_peers(session, mode, ticker, sic or (sec.sic_code if sec else None), mcap or (sec.market_cap if sec else None),
                           r.get("net_margin"), r.get("revenue_growth_yoy"))

    eps = t.get("eps")
    current_pe = price / eps if eps and eps > 0 and (r.get("net_margin") or 0) >= 0.02 and state == "normal" else None
    pe_sel = select_multiples("pe", current_pe, history.get("pe"), peers.bands.get("pe") if peers else None,
                              (sector.pe_low, sector.pe_mid, sector.pe_high))
    # revenue multiple: primary for unprofitable companies; otherwise the fallback for scenarios whose
    # projected EPS is not positive — always the company's own (or peers') P/S, never a sector band
    ps_sel = None
    if t.get("revenue") and shares_current:
        ps_sel = select_multiples("ps", price * shares_current / t["revenue"], history.get("ps"),
                                  peers.bands.get("ps") if peers else None, None, bounds=PS_BOUNDS)

    book_model = None
    if plan.method in ("bank_ptbv_roe", "insurance_pb_roe") and shares_current and (t.get("book_value") or 0) > 0:
        bank = plan.method == "bank_ptbv_roe"
        tb = t.get("tangible_book_value")
        use_tb = bank and tb is not None and tb > 0
        measure = "tangible book" if use_tb else "book"
        bv = tb if use_tb else t["book_value"]
        roe_hist = [h.get("roe") for h in snap.margin_history]
        r_ttm = r.get("roe")
        r_norm, r_basis = normalized_return(roe_hist, r_ttm, blend_ttm=bank)
        if r_norm is not None and use_tb and t["book_value"] > 0:
            r_norm *= t["book_value"] / bv  # ROTCE ~ ROE x book / tangible book (current ratio)
            r_basis += "; converted to return on tangible equity using the current book / tangible book ratio"
        if r_norm is not None:
            payout = r.get("payout_ratio")
            retention = 1 - min(max(payout, 0.0), 1.0) if payout is not None else DEFAULT_RETENTION
            ret_basis = (f"1 - reported dividend payout ({payout:.0%})" if payout is not None
                         else f"{DEFAULT_RETENTION:.0%} assumed (dividends not reported)")
            ke = cost_of_equity(settings.risk_free_rate, min(max(beta or 1.0, 0.6), 2.0), settings.equity_risk_premium)
            just = justified_multiple(r_norm, ke, retention)
            hist_band = history.get("ptbv" if use_tb else "pb")
            peer_band = peers.bands.get("pb") if peers and not use_tb else None
            sel = select_multiples("ptbv" if use_tb else "pb", price / (bv / shares_current), hist_band, peer_band,
                                   None, fundamental=just, bounds=PB_BOUNDS)
            if sel is not None:
                book_model = BookValueModel("bank" if bank else "insurance", measure, bv / shares_current, r_norm,
                                            r_basis, retention, ret_basis, ke, just, sel, list(plan.notes))
    ffo_model = None
    if is_reit and ffo_ps is not None and ffo_ps > 0:
        sel = select_multiples("pffo", price / ffo_ps, history.get("pffo"), None, REIT_PFFO_REFERENCE, bounds=PFFO_BOUNDS)
        if sel is not None:
            ffo_model = FFOModel(ffo_ps, ffo_basis, sel, list(plan.notes))
    if plan.method in ("bank_ptbv_roe", "insurance_pb_roe") and book_model is None:
        plan.required["normalized return on equity"] = False
        plan.status_hint = "unsupported"
    if is_reit and ffo_model is None:
        plan.required["positive FFO"] = False
        plan.status_hint = "unsupported"
    if plan.status_hint == "valid" and (
            (pe_sel is not None and pe_sel.limited and plan.method not in ("reit_pffo",) and "forward_pe" in plan.weights
             and plan.weights.get("forward_pe", 0) >= 0.5)
            or (ffo_model is not None and ffo_model.selection.limited)
            or (book_model is not None and book_model.selection.limited)
            or (plan.method.startswith("revenue") and ps_sel is None)
            or plan.notes):
        plan.status_hint = "limited"

    inp = ForecastInputs(
        ticker=ticker, price=price, price_date=price_date, sector=sector,
        revenue_ttm=t.get("revenue"), revenue_growth_yoy=r.get("revenue_growth_yoy"),
        revenue_cagr_3y=r.get("revenue_cagr_3y"), net_margin=r.get("net_margin"),
        operating_margin=r.get("operating_margin"), fcf_margin=r.get("fcf_margin"),
        eps_ttm=eps, shares=t.get("diluted_shares") or shares_current,
        share_change_rate=r.get("share_change_rate"), cash=t.get("cash_and_investments"), debt=t.get("total_debt"),
        margin_history=snap.margin_history, beta=beta, volatility=volatility, currency_ok=currency_ok,
        risk_free_rate=settings.risk_free_rate, equity_risk_premium=settings.equity_risk_premium,
        pre_tax_cost_of_debt=settings.pre_tax_cost_of_debt, tax_rate=settings.tax_rate,
        ai_adjustments=ai_adjustments or [], plan=plan, pe_selection=pe_sel, ps_selection=ps_sel,
        book_model=book_model, ffo_model=ffo_model, fcf_after_sbc_margin=r.get("fcf_after_sbc_margin"),
        capex_ratio=r.get("capex_ratio"), sbc_ratio=r.get("sbc_ratio"),
        target_margin=peers.profitable_margin if peers and plan.method == "revenue_margin_path" else None,
    )
    ctx = ValuationContext(plan, history, peers, pe_sel, ps_sel, book_model, ffo_model, mcap, basis, shares_current,
                           share_basis_ok, notes)
    return inp, ctx


def assess(ctx: ValuationContext, h12: HorizonForecast, snap: FinancialSnapshot, engine_flags: list[str],
           settings) -> GuardrailResult:
    base = h12.scenarios["base"]
    kind = ctx.primary_kind()
    if kind == "book":
        sel, mult = ctx.book_model.selection, (base.details.get("book_value_model") or {}).get("multiple")
    elif kind == "ffo":
        sel, mult = ctx.ffo_model.selection, (base.details.get("ffo_model") or {}).get("multiple")
    elif kind == "ps":
        sel, mult = ctx.ps_selection, base.details.get("price_to_sales_multiple")
    else:
        sel, mult = ctx.pe_selection, base.pe_multiple
    band = (sel.evidence.get("company_history") if sel else None)
    if band and band.get("n", 0) < 6:
        band = None
    return evaluate(
        base_return=base.potential_return, base_multiple=mult, current_multiple=sel.current if sel else None,
        company_band=band, selection_basis=sel.basis if sel else None,
        selection_limited=sel is None or sel.limited,
        method_ratio=base.details.get("method_ratio"), engine_flags=engine_flags, plan_status=ctx.plan.status_hint,
        plan_missing=ctx.plan.missing, financial_status=snap.financial_status, validation=snap.validation,
        withheld="withheld_target" in base.details, share_basis_ok=ctx.share_basis_ok,
        upside_threshold=settings.valuation_upside_review_threshold,
        max_rerating=settings.valuation_max_unsupported_rerating,
    )


def audit_record(ctx: ValuationContext, guard: GuardrailResult, h12: HorizonForecast, snap: FinancialSnapshot,
                 price_date: date, model_version: str) -> dict[str, Any]:
    scen = {}
    for name, s in h12.scenarios.items():
        d = s.details
        scen[name] = {
            "revenue": s.revenue, "revenue_growth": s.revenue_growth_annual, "net_margin": s.net_margin,
            "net_income": d.get("net_income"), "eps": s.eps, "free_cash_flow": s.free_cash_flow,
            "capex": d.get("capex"), "stock_based_comp": d.get("stock_based_comp"), "shares": d.get("shares"),
            "pe_multiple": s.pe_multiple, "book_value_model": d.get("book_value_model"), "ffo_model": d.get("ffo_model"),
            "price_to_sales_multiple": d.get("price_to_sales_multiple"),
            "dcf": {k: v for k, v in (d.get("dcf") or {}).items() if k != "projected_fcfs"} or None,
            "method_weights": d.get("method_weights"), "method_targets": d.get("method_targets"),
            "target_price": s.target_price, "potential_return": s.potential_return,
            "withheld_target": d.get("withheld_target"), "target_date": str(h12.target_date),
        }
    t = snap.ttm
    return {
        "status": guard.status, "status_label": guard.to_dict()["label"], "model_version": model_version,
        "method": ctx.plan.method, "method_label": ctx.plan.label, "business_model": ctx.plan.business_model,
        "rationale": ctx.plan.rationale, "required_inputs": ctx.plan.required, "missing_inputs": ctx.plan.missing,
        "plan_notes": ctx.plan.notes, "weights": ctx.plan.weights,
        "historical_multiples": {k: b.to_dict() for k, b in ctx.history.items()},
        "peers": ctx.peers.to_dict() if ctx.peers else None,
        "multiples": {
            "pe": ctx.pe_selection.to_dict() if ctx.pe_selection else None,
            "ps": ctx.ps_selection.to_dict() if ctx.ps_selection else None,
            "book": ctx.book_model.describe() if ctx.book_model else None,
            "ffo": ctx.ffo_model.describe() if ctx.ffo_model else None,
        },
        "earnings_state": h12.methods.get("earnings_state"), "regime": h12.methods.get("regime"),
        "scenarios_12m": scen, "guardrails": guard.to_dict(),
        "market_cap": {"value": ctx.market_cap, "basis": ctx.market_cap_basis, "shares": ctx.shares_current},
        "financial_status": snap.financial_status, "validation": snap.validation,
        "split_adjustments": snap.split_adjustments,
        "financial_coverage": {k: (t.get(k) is not None) for k in (
            "revenue", "net_income", "operating_cash_flow", "capex_total", "share_based_comp", "free_cash_flow",
            "book_value", "tangible_book_value", "shares_outstanding", "diluted_shares", "depreciation")},
        "provenance": snap.provenance,
        "data_timestamps": {"price_date": str(price_date), "latest_filing": str(snap.latest_filed) if snap.latest_filed else None,
                            "ttm_period_end": str(snap.ttm_period_end) if snap.ttm_period_end else None},
        "notes": ctx.notes,
    }
