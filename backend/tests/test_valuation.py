"""Financial-accuracy tests: company-specific multiples, sector valuation models, guardrails, SEC
extraction, share counts / market cap and discovery integration. Deterministic, no network."""

from datetime import date, datetime

import pandas as pd
import pytest

from app.providers.sec.xbrl import FinancialFact, normalize_companyfacts
from app.services.discovery_scoring import factor_values
from app.services.financials import build_snapshot, capex_total, split_factor
from app.services.forecasting.company_multiples import (
    MultipleBand,
    earnings_state,
    historical_multiples,
    select_multiples,
)
from app.services.forecasting.engine import ForecastEngine, ForecastInputs
from app.services.forecasting.sector_valuation import (
    BookValueModel,
    FFOModel,
    ffo_per_share,
    justified_multiple,
)
from app.services.forecasting.sectors import profile
from app.services.forecasting.valuation_guardrails import (
    INVALID,
    LIMITED,
    REVIEW,
    UNSUPPORTED,
    VALID,
    evaluate,
    ranking_exclusion,
)
from app.services.forecasting.valuation_registry import business_model, choose_plan
from app.services.market_screen import combine_revenue_components, eligibility_codes, fundamental_metrics
from app.services.screen_coverage import fallback_metrics, resolve_market_cap

TODAY = date(2026, 10, 13)


def band(p25, p50, p75, n=24, kind="pe", source="company_history"):
    return MultipleBand(kind, p25 * 0.9, p25, p50, p75, p75 * 1.1, n=n, source=source)


def inputs(**kw) -> ForecastInputs:
    base = dict(
        ticker="TEST", price=100.0, price_date=TODAY, sector=profile("Technology"),
        revenue_ttm=10e9, revenue_growth_yoy=0.08, revenue_cagr_3y=0.07, net_margin=0.15, operating_margin=0.2,
        fcf_margin=0.14, eps_ttm=5.0, shares=0.3e9, share_change_rate=0.0, cash=1e9, debt=1e9,
        margin_history=[{"revenue": r, "net_margin": 0.15, "fcf_margin": 0.14} for r in (8e9, 8.6e9, 9.3e9, 10e9)],
        beta=1.0, volatility=0.3,
    )
    base.update(kw)
    return ForecastInputs(**base)


# ============================================================ Forward P/E: multiple selection
def test_company_history_drives_multiples_not_the_sector_band():
    sel = select_multiples("pe", 5.0, band(4.0, 5.0, 6.0), None, (18, 27, 38))
    assert sel.basis == "company_history" and not sel.limited
    assert sel.base_anchor == pytest.approx(5.0) and sel.bull <= 6.6  # never the 27x sector midpoint


def test_peer_evidence_is_supporting_and_bounded():
    sel = select_multiples("pe", 4.8, band(3.9, 4.8, 5.5), band(20, 25, 35, kind="pe", source="peers"), (15, 20, 27))
    assert sel.basis == "company_and_peers"
    assert sel.base_anchor <= 4.8 * 1.25 + 1e-9  # peers can nudge, not replace, the company's own valuation
    assert any("limited to" in n for n in sel.notes)


def test_sector_band_is_a_marked_last_resort():
    sel = select_multiples("pe", 20.0, None, None, (18, 27, 38))
    assert sel.basis == "sector_reference" and sel.limited
    short = select_multiples("pe", 20.0, band(10, 12, 14, n=3), None, (18, 27, 38))
    assert short.basis == "sector_reference" and any("only 3" in n for n in short.notes)


def test_base_multiple_moves_only_part_way_toward_the_anchor():
    sel = select_multiples("pe", 8.0, band(10.0, 13.0, 18.0), None, None)
    one_year = sel.base_at(1.0)
    assert 8.0 < one_year < 13.0
    assert (one_year - 10.0 * 0 - sel.low) >= 0  # starts from today's multiple bounded by the evidence range
    assert sel.base_at(1.0) < sel.base_at(5.0) <= 13.0
    assert sel.bear <= 8.0  # bear never assumes expansion from today's multiple


def test_negative_and_near_zero_eps_months_are_rejected_from_history():
    def ff(metric, end, val, filed, ptype="annual"):
        return FinancialFact(metric, ptype, date(end.year, 1, 1), end, val, "USD", "X", "us-gaap", f"A{end.year}",
                             "10-K", filed, end.year, "FY")
    facts = [ff("revenue", date(2023, 12, 31), 100.0, date(2024, 2, 15)), ff("net_income", date(2023, 12, 31), -10.0, date(2024, 2, 15)),
             ff("revenue", date(2024, 12, 31), 100.0, date(2025, 2, 15)), ff("net_income", date(2024, 12, 31), 20.0, date(2025, 2, 15)),
             ff("diluted_shares", date(2023, 12, 31), 10.0, date(2024, 2, 15)), ff("diluted_shares", date(2024, 12, 31), 10.0, date(2025, 2, 15))]
    idx = pd.date_range("2024-01-31", "2025-12-31", freq="ME")
    prices = pd.DataFrame({"close": [100.0] * len(idx)}, index=idx)
    pe = historical_multiples(facts, prices)["pe"]
    assert pe.p50 == pytest.approx(50.0)  # only the profitable months: price 100 / EPS 2
    assert pe.rejected >= 12 and pe.n == len(idx) - pe.rejected


def test_negative_eps_uses_company_revenue_multiple_not_pe():
    ps = select_multiples("ps", 1.0, band(0.8, 1.0, 1.4, kind="ps"), None, None, bounds=(0.1, 30))
    h = ForecastEngine(inputs(net_margin=-0.10, eps_ttm=-3.0, fcf_margin=-0.05, margin_history=[], ps_selection=ps)).run(12)
    base = h.scenarios["base"]
    assert h.methods["regime"] == "sales" and base.target_price_pe is None
    assert base.details["revenue_multiple_basis"] == "company_history"


def test_near_zero_eps_with_profitable_history_is_valued_on_normalized_earnings():
    hist = [{"revenue": 9e9, "net_margin": m, "fcf_margin": 0.08} for m in (0.08, 0.09, 0.10, 0.02, 0.01)]
    eng = ForecastEngine(inputs(net_margin=0.005, eps_ttm=0.17, margin_history=hist))
    h = eng.run(12)
    assert eng.regime == "normalized_earnings" and eng.earnings_state == "depressed"
    base = h.scenarios["base"]
    assert base.details["normalized_margin"] == pytest.approx(0.08)  # median of the company's own margins
    assert base.target_price_pe == pytest.approx(base.details["normalized_eps"] * base.pe_multiple)
    assert "eps_depressed" in eng.flags


def test_earnings_spike_is_valued_on_normalized_eps():
    hist = [{"revenue": 9e9, "net_margin": m, "fcf_margin": 0.1} for m in (0.10, 0.11, 0.12, 0.10, 0.11)]
    eng = ForecastEngine(inputs(net_margin=0.30, eps_ttm=10.0, margin_history=hist))
    assert eng.earnings_state == "inflated"
    base = eng.run(12).scenarios["base"]
    assert base.details["normalized_margin"] == pytest.approx(0.11)
    # a level sustained over the last two fiscal years is structural, not a spike
    hist2 = hist[:3] + [{"revenue": 9e9, "net_margin": 0.29, "fcf_margin": 0.1}, {"revenue": 9e9, "net_margin": 0.31, "fcf_margin": 0.1}]
    assert earnings_state(0.30, 0.12, [h["net_margin"] for h in hist2]) == "normal"


def test_cyclical_growth_is_not_extrapolated():
    hist = [{"revenue": 9e9, "net_margin": m, "fcf_margin": 0.1} for m in (0.1, -0.05, 0.2, 0.05)]
    eng = ForecastEngine(inputs(revenue_growth_yoy=0.55, revenue_cagr_3y=0.48, margin_history=hist))
    assert eng.cyclical and eng.g_hist == pytest.approx(0.15)


def test_growth_disagreement_uses_conservative_blend():
    eng = ForecastEngine(inputs(revenue_growth_yoy=0.81, revenue_cagr_3y=0.07))
    assert eng.g_hist == pytest.approx(0.07 + 0.25 * 0.74) and "growth_inconsistent" in eng.flags


def test_scenarios_share_one_method_mix_and_stay_ordered():
    h = ForecastEngine(inputs(fcf_margin=0.35, margin_history=[])).run(12)
    mixes = {tuple(sorted(h.scenarios[s].details["method_weights"])) for s in ("bear", "base", "bull")}
    assert len(mixes) == 1
    t = [h.scenarios[s].target_price for s in ("bear", "base", "bull")]
    assert t[0] <= t[1] <= t[2]


def test_recovery_dependent_upside_requires_review():
    assert _eval(base_return=0.7, base_multiple=11.0, engine_flags=["eps_depressed"]).status == REVIEW
    assert _eval(base_return=0.2, base_multiple=11.0, engine_flags=["eps_depressed"]).status == VALID


def test_dcf_fades_out_when_methods_disagree():
    s = ForecastEngine(inputs(fcf_margin=0.6, margin_history=[])).run(12).scenarios["base"]
    assert s.details["method_ratio"] > 3.0
    assert "dcf" not in s.details["method_weights"] and "dcf" in s.details["method_targets"]  # cross-check only


# ============================================================ guardrails
def _eval(**kw):
    base = dict(base_return=0.3, base_multiple=12.0, current_multiple=11.0, company_band={"p25": 10, "p50": 12, "p75": 14, "n": 24},
                selection_basis="company_history", selection_limited=False, method_ratio=1.2, engine_flags=[],
                plan_status="valid", plan_missing=[], financial_status="valid", validation=[], withheld=False,
                share_basis_ok=True)
    base.update(kw)
    return evaluate(**base)


def test_unjustified_multiple_expansion_requires_review():
    g = _eval(base_return=1.5, base_multiple=12.0, current_multiple=5.0, company_band={"p25": 4, "p50": 5, "p75": 6, "n": 24})
    assert g.status == REVIEW
    codes = {f.code: f.severity for f in g.flags}
    assert codes["multiple_expansion"] == "review" and codes["unsupported_upside"] == "review"


def test_supported_large_upside_is_kept_not_capped():
    g = _eval(base_return=1.3, base_multiple=11.5, current_multiple=9.0)
    assert g.status == VALID and g.upside_check["supported"] is True
    assert g.upside_check["base_return_12m"] == 1.3  # never capped
    assert any(f.code == "large_upside_supported" for f in g.flags)


def test_large_upside_from_depressed_earnings_needs_review():
    assert _eval(base_return=1.2, base_multiple=11.0, engine_flags=["eps_depressed"]).status == REVIEW


def test_invalid_and_unsupported_statuses():
    assert _eval(withheld=True).status == INVALID
    assert _eval(share_basis_ok=False).status == INVALID
    assert _eval(financial_status="invalid", validation=["Net income exceeds three times revenue"]).status == INVALID
    assert _eval(plan_status="unsupported", plan_missing=["FFO inputs"]).status == UNSUPPORTED
    assert _eval(base_return=None).status == UNSUPPORTED
    assert _eval(selection_limited=True).status == LIMITED


def test_disagreement_only_blocks_attractive_results():
    assert _eval(method_ratio=4.0, base_return=0.1).status == VALID
    assert _eval(method_ratio=4.0, base_return=0.8).status == REVIEW


# ============================================================ sector valuation models
@pytest.mark.parametrize("sic, sector, desc, margin, eps, growth, expected", [
    ("7372", "Technology", None, 0.25, 5.0, 0.15, "earnings_dcf"),
    ("6022", "Financials", None, 0.3, 4.0, 0.05, "bank_ptbv_roe"),
    ("6331", "Financials", "fire, marine & casualty insurance", 0.12, 9.0, 0.06, "insurance_pb_roe"),
    ("4512", "Industrials", "air transportation, scheduled", 0.08, 6.0, 0.05, "airline_normalized"),
    ("4911", "Utilities", None, 0.12, 3.0, 0.04, "utility_dcf_pe"),
    ("6798", "Real Estate", None, 0.3, 1.0, 0.04, "reit_pffo"),
    ("2834", "Health Care", None, -0.4, -2.0, 0.35, "revenue_margin_path"),
])
def test_method_registry_by_business_model(sic, sector, desc, margin, eps, growth, expected):
    plan = choose_plan(sic=sic, sector=sector, description=desc, net_margin=margin, eps=eps, margin_median=None,
                       margin_n=0, revenue_growth=growth, cyclical=False, currency_ok=True, has_revenue=True,
                       has_shares=True, book_value=1e9, tangible_book=8e8, goodwill_reported=True, ffo_available=True,
                       ffo_gains_reported=True, fcf_positive=True)
    assert plan.method == expected and plan.status_hint == "valid" and not plan.missing


def test_methods_are_only_activated_with_their_inputs():
    reit = choose_plan(sic="6798", sector="Real Estate", description=None, net_margin=0.2, eps=1.0, margin_median=None,
                       margin_n=0, revenue_growth=0.03, cyclical=False, currency_ok=True, has_revenue=True, has_shares=True,
                       book_value=1e9, tangible_book=None, goodwill_reported=False, ffo_available=False,
                       ffo_gains_reported=False, fcf_positive=True)
    assert reit.status_hint == "unsupported" and reit.missing  # no FFO is fabricated
    util = choose_plan(sic="4911", sector="Utilities", description=None, net_margin=0.1, eps=3.0, margin_median=0.1,
                       margin_n=5, revenue_growth=0.04, cyclical=False, currency_ok=True, has_revenue=True, has_shares=True,
                       book_value=1e9, tangible_book=1e9, goodwill_reported=True, ffo_available=False,
                       ffo_gains_reported=False, fcf_positive=False)
    assert util.weights == {"forward_pe": 1.0}  # negative FCF (capex program): DCF dropped
    airline = choose_plan(sic="4512", sector="Industrials", description=None, net_margin=0.08, eps=6.0, margin_median=0.05,
                          margin_n=5, revenue_growth=0.05, cyclical=True, currency_ok=True, has_revenue=True, has_shares=True,
                          book_value=1e9, tangible_book=1e9, goodwill_reported=True, ffo_available=False,
                          ffo_gains_reported=False, fcf_positive=True)
    assert any("EBITDAR" in n for n in airline.notes)
    adr = choose_plan(sic="7372", sector="Technology", description=None, net_margin=0.2, eps=2.0, margin_median=None,
                      margin_n=0, revenue_growth=0.1, cyclical=False, currency_ok=False, has_revenue=True, has_shares=True,
                      book_value=1e9, tangible_book=1e9, goodwill_reported=True, ffo_available=False,
                      ffo_gains_reported=False, fcf_positive=True)
    assert adr.status_hint == "unsupported"
    assert business_model("6099", "Financials") == "other_financial"


def test_justified_book_multiple_uses_financial_cost_of_equity_floor():
    assert justified_multiple(0.15, 0.07, 1.0) == pytest.approx((0.15 - 0.04) / (0.09 - 0.04))
    assert justified_multiple(0.40, 0.09, 1.0) == pytest.approx(3.0)  # bounded


def test_bank_book_value_model_targets():
    sel = select_multiples("ptbv", 1.2, band(1.0, 1.3, 1.6, kind="ptbv"), None, None, fundamental=1.5, bounds=(0.3, 6))
    m = BookValueModel("bank", "tangible book", 50.0, 0.14, "median", 0.6, "payout", 0.1, 1.5, sel)
    price, d = m.target("base", 1.0)
    assert d["tangible book_per_share"] == pytest.approx(50 * (1 + 0.14 * 0.6))
    assert price == pytest.approx(d["tangible book_per_share"] * sel.base_at(1.0))
    assert m.target("bear", 1.0)[0] < price < m.target("bull", 1.0)[0]


def test_insurer_engine_blends_book_model_and_pe_without_dcf():
    sel = select_multiples("pb", 0.9, band(0.85, 1.0, 1.2, kind="pb"), None, None, fundamental=1.4, bounds=(0.3, 6))
    bm = BookValueModel("insurance", "book", 2000.0, 0.12, "median", 0.95, "payout", 0.09, 1.4, sel)
    plan = choose_plan(sic="6331", sector="Financials", description=None, net_margin=0.2, eps=300.0, margin_median=None,
                       margin_n=0, revenue_growth=0.05, cyclical=False, currency_ok=True, has_revenue=True, has_shares=True,
                       book_value=5e9, tangible_book=5e9, goodwill_reported=True, ffo_available=False,
                       ffo_gains_reported=False, fcf_positive=True)
    h = ForecastEngine(inputs(sector=profile("Financials"), price=1800.0, eps_ttm=300.0, net_margin=0.2, shares=2.5e6,
                              revenue_ttm=3.75e9, plan=plan, book_model=bm)).run(12)
    w = h.scenarios["base"].details["method_weights"]
    assert set(w) == {"book_value_model", "forward_pe"} and w["book_value_model"] == pytest.approx(0.7)
    assert h.scenarios["base"].target_price_dcf is None


def test_reit_ffo_is_derived_only_from_reported_inputs():
    assert ffo_per_share({"net_income_to_common": 100.0, "depreciation": 50.0, "gain_on_property_sales": 10.0,
                          "diluted_shares": 10.0})[0] == pytest.approx(14.0)
    assert ffo_per_share({"net_income_to_common": 100.0, "depreciation": None, "diluted_shares": 10.0})[0] is None
    sel = select_multiples("pffo", 14.0, None, None, (12.0, 16.0, 20.0), bounds=(5, 40))
    plan = choose_plan(sic="6798", sector="Real Estate", description=None, net_margin=0.3, eps=1.0, margin_median=None,
                       margin_n=0, revenue_growth=0.03, cyclical=False, currency_ok=True, has_revenue=True, has_shares=True,
                       book_value=1e9, tangible_book=1e9, goodwill_reported=True, ffo_available=True,
                       ffo_gains_reported=True, fcf_positive=True)
    h = ForecastEngine(inputs(sector=profile("Real Estate"), plan=plan, ffo_model=FFOModel(7.0, "x", sel))).run(12)
    base = h.scenarios["base"]
    assert base.details["method_weights"] == {"ffo_model": 1.0} and base.target_price_pe is None
    assert sel.limited  # sector P/FFO reference without company history => limited


# ============================================================ SEC extraction
def ff(metric, ptype, start, end, val, filed, concept="X", accn=None, unit="USD"):
    return FinancialFact(metric, ptype, start, end, val, unit, concept, "us-gaap", accn or f"{metric}-{end}",
                         "10-K" if ptype == "annual" else "10-Q", filed, end.year, "FY" if ptype == "annual" else None)


def _quarters(metric, vals, year=2025, concept="X"):
    ends = [date(year, 3, 31), date(year, 6, 30), date(year, 9, 30), date(year, 12, 31)]
    starts = [date(year, 1, 1), date(year, 4, 1), date(year, 7, 1), date(year, 10, 1)]
    return [ff(metric, "quarterly", s, e, v, date(e.year + (e.month == 12), (e.month % 12) + 1, 15), concept)
            for s, e, v in zip(starts, ends, vals, strict=True)]


def test_bank_revenue_from_net_interest_and_noninterest_income():
    facts = (_quarters("net_interest_income", [10, 10, 11, 11]) + _quarters("noninterest_income", [5, 5, 5, 6])
             + _quarters("net_income", [4, 4, 4, 5]))
    snap = build_snapshot(facts, as_of=TODAY)
    assert snap.ttm["revenue"] == pytest.approx(63.0)
    assert any("net interest income + noninterest income" in w for w in snap.warnings)


def test_insurance_revenue_from_premiums_and_investment_income():
    facts = _quarters("premiums_earned", [80] * 4) + _quarters("net_investment_income", [5] * 4) + _quarters("net_income", [9] * 4)
    assert build_snapshot(facts, as_of=TODAY).ttm["revenue"] == pytest.approx(340.0)


def test_total_revenue_concept_is_never_mixed_with_components():
    facts = _quarters("revenue", [100] * 4) + _quarters("net_interest_income", [10] * 4) + _quarters("noninterest_income", [5] * 4)
    assert build_snapshot(facts, as_of=TODAY).ttm["revenue"] == pytest.approx(400.0)


def test_net_income_attributable_to_common_shareholders():
    facts = (_quarters("revenue", [100] * 4) + _quarters("net_income", [10] * 4)
             + _quarters("preferred_dividends", [1] * 4)
             + [ff("diluted_shares", "quarterly", date(2025, 10, 1), date(2025, 12, 31), 10.0, date(2026, 1, 15))])
    snap = build_snapshot(facts, as_of=TODAY)
    assert snap.ttm["net_income_to_common"] == pytest.approx(36.0) and snap.ttm["eps"] == pytest.approx(3.6)


def test_stale_component_is_not_mixed_into_current_figures():
    """Regression: a 'net income to common' concept last tagged years ago must not replace current earnings."""
    facts = (_quarters("revenue", [100] * 4) + _quarters("net_income", [10] * 4)
             + [ff("net_income_common", "annual", date(2021, 7, 1), date(2022, 6, 30), 5.0, date(2022, 8, 5)),
                ff("diluted_shares", "quarterly", date(2025, 10, 1), date(2025, 12, 31), 10.0, date(2026, 1, 15))])
    snap = build_snapshot(facts, as_of=TODAY)
    assert snap.ttm["net_income_common"] is None and snap.ttm["eps"] == pytest.approx(4.0)
    assert snap.provenance["net_income_common"]["ignored"] == "stale"


def test_consolidated_profit_fallback_is_flagged():
    facts = _quarters("revenue", [100] * 4) + _quarters("net_income", [10] * 4, concept="ProfitLoss")
    snap = build_snapshot(facts, as_of=TODAY)
    assert any("ProfitLoss" in w for w in snap.warnings)


def test_ttm_and_annual_basis_is_recorded():
    q = build_snapshot(_quarters("revenue", [100] * 4), as_of=TODAY)
    assert q.provenance["revenue"]["basis"] == "ttm_4_quarters" and q.ttm["revenue"] == 400
    a = build_snapshot([ff("revenue", "annual", date(2025, 1, 1), date(2025, 12, 31), 380.0, date(2026, 2, 15))], as_of=TODAY)
    assert a.provenance["revenue"]["basis"] == "fiscal_year" and a.provenance["revenue"]["filed"] == "2026-02-15"


def test_ytd_cash_flow_components_are_derived_into_quarters():
    def raw(start, end, val, accn, form, filed, fp):
        return {"start": start, "end": end, "val": val, "accn": accn, "form": form, "filed": filed, "fy": 2026, "fp": fp}
    flight = [raw("2026-01-01", "2026-03-31", 100, "Q1", "10-Q", "2026-05-01", "Q1"),
              raw("2026-01-01", "2026-06-30", 260, "Q2", "10-Q", "2026-08-01", "Q2")]
    facts = normalize_companyfacts({"facts": {"us-gaap": {"PaymentsForFlightEquipment": {"units": {"USD": flight}}}}})
    q = sorted((x.period_end, x.value) for x in facts if x.metric == "capex_flight_equipment" and x.period_type == "quarterly")
    assert q == [(date(2026, 3, 31), 100), (date(2026, 6, 30), 160)]  # Q2 = 6M YTD - Q1, never YTD as a quarter


def test_capex_includes_separately_tagged_aircraft_and_fcf_after_sbc():
    assert capex_total(None, [20.0, 200.0, None]) == pytest.approx(220.0)
    assert capex_total(250.0, [20.0, 200.0]) == pytest.approx(250.0)  # a reported total wins over its parts
    facts = (_quarters("revenue", [1000] * 4) + _quarters("operating_cash_flow", [200] * 4) + _quarters("capex", [5] * 4)
             + _quarters("capex_flight_equipment", [50] * 4) + _quarters("share_based_comp", [10] * 4))
    t = build_snapshot(facts, as_of=TODAY).ttm
    assert t["capex_total"] == pytest.approx(220.0) and t["free_cash_flow"] == pytest.approx(580.0)
    assert t["free_cash_flow_after_sbc"] == pytest.approx(540.0)


def test_split_adjusts_only_facts_filed_before_the_split():
    splits = [(date(2026, 6, 12), 10.0)]
    assert split_factor(splits, date(2026, 3, 31), date(2026, 4, 30)) == 10.0
    assert split_factor(splits, date(2026, 6, 30), date(2026, 8, 6)) == 1.0  # already restated by the filer
    assert split_factor(splits, date(2026, 3, 31), date(2026, 8, 6)) == 1.0  # comparative in a post-split filing
    facts = (_quarters("revenue", [100] * 4) + _quarters("net_income", [10] * 4)
             + [ff("diluted_shares", "quarterly", date(2025, 10, 1), date(2025, 12, 31), 10.0, date(2026, 1, 15)),
                ff("shares_outstanding", "instant", None, date(2026, 1, 20), 10.0, date(2026, 1, 25), unit="shares")])
    snap = build_snapshot(facts, as_of=TODAY, splits=splits)
    assert snap.ttm["diluted_shares"] == 100.0 and snap.ttm["eps"] == pytest.approx(0.4)
    assert snap.split_adjustments


def test_inconsistent_and_missing_data_are_classified_not_scored_as_weak():
    bad = build_snapshot(_quarters("revenue", [10] * 4) + _quarters("net_income", [100] * 4), as_of=TODAY)
    assert bad.financial_status == "invalid"
    assert build_snapshot([], as_of=TODAY).financial_status == "data_unavailable"
    assert build_snapshot(_quarters("revenue", [100] * 4) + _quarters("net_income", [-30] * 4), as_of=TODAY).financial_status == "valid"
    # early-stage: losses far larger than revenue are real, not a data error
    assert build_snapshot(_quarters("revenue", [10] * 4) + _quarters("net_income", [-100] * 4), as_of=TODAY).financial_status == "valid"
    zero = build_snapshot(_quarters("revenue", [10] * 4) + [ff("diluted_shares", "quarterly", date(2025, 10, 1), date(2025, 12, 31), 5.0, date(2026, 1, 15)),
                                                            ff("shares_outstanding", "instant", None, date(2026, 1, 20), 0.0, date(2026, 1, 25), unit="shares")], as_of=TODAY)
    assert zero.ttm["shares_outstanding"] is None and any("non-positive" in v for v in zero.validation)


def test_companyfacts_fallback_metrics_keep_provenance():
    def raw(start, end, val, accn, form, filed, fp):
        d = {"end": end, "val": val, "accn": accn, "form": form, "filed": filed, "fy": 2026, "fp": fp}
        if start:
            d["start"] = start
        return d
    cf = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [raw("2025-01-01", "2025-12-31", 1000, "K25", "10-K", "2026-02-10", "FY")]}},
        "NetIncomeLoss": {"units": {"USD": [raw("2025-01-01", "2025-12-31", 80, "K25", "10-K", "2026-02-10", "FY")]}},
    }, "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [raw(None, "2026-02-01", 50, "K25", "10-K", "2026-02-10", "FY")]}}}}}
    m = fallback_metrics(cf)
    assert m["revenue"]["val"] == 1000 and m["revenue"]["accn"] == "K25" and m["revenue"]["source"] == "companyfacts"
    assert m["shares_outstanding"]["val"] == 50 and m["_status"] == "valid"


# ============================================================ share counts and market cap
def _m(**shares):
    return {k: {"val": v[0], "end": v[1]} for k, v in shares.items()}


def test_market_cap_prefers_provider_then_sec_cover_page():
    mc = resolve_market_cap(50.0, _m(shares_outstanding=(1e8, "2026-07-31")), None, 1.02e8, datetime(2026, 10, 1), None, None, TODAY)
    assert mc.source == "provider" and mc.value == pytest.approx(50 * 1.02e8)
    mc = resolve_market_cap(50.0, _m(shares_outstanding=(1e8, "2026-07-31")), None, None, None, None, None, TODAY)
    assert mc.source == "shares_outstanding" and mc.value == pytest.approx(5e9)
    mc = resolve_market_cap(50.0, _m(shares_outstanding_bs=(1e8, "2026-06-30")), None, None, None, None, None, TODAY)
    assert mc.source == "shares_outstanding_bs"
    fb = {"shares_outstanding": {"val": 1e8, "end": "2026-07-01"}}
    assert resolve_market_cap(50.0, {}, fb, None, None, None, None, TODAY).source == "fallback"


def test_market_cap_split_after_share_count_date():
    mc = resolve_market_cap(20.0, _m(shares_outstanding=(1.3e8, "2026-04-27")), None, None, None,
                            [(date(2026, 6, 12), 10.0)], None, TODAY)
    assert mc.shares == pytest.approx(1.3e9) and mc.value == pytest.approx(2.6e10)
    assert any(f.startswith("split_adjusted") for f in mc.flags)


def test_stale_and_missing_shares():
    stale = resolve_market_cap(10.0, _m(shares_outstanding=(1e8, "2026-01-15")), None, None, None, None, None, TODAY)
    assert stale.value is not None and "stale_shares" in stale.flags
    too_old = resolve_market_cap(10.0, _m(shares_outstanding=(1e8, "2025-06-30")), None, None, None, None, None, TODAY)
    assert too_old.value is None and too_old.reason
    missing = resolve_market_cap(10.0, {}, None, None, None, None, None, TODAY)
    assert missing.value is None and "no shares" in missing.reason


def test_multiple_share_classes():
    m = _m(shares_outstanding_bs=(12.2e9, "2026-06-30"))
    ok = resolve_market_cap(200.0, m, None, None, None, None, [200.0, 202.0], TODAY)  # e.g. two similar classes
    assert ok.value == pytest.approx(200.0 * 12.2e9) and "multi_class" in ok.flags
    diff = resolve_market_cap(500.0, m, None, None, None, None, [500.0, 750000.0], TODAY)  # e.g. A/B shares at 1500:1
    assert diff.value is None and "class-aware" in diff.reason
    prov = resolve_market_cap(500.0, m, None, 2.1e9, datetime(2026, 10, 1), None, [500.0, 750000.0], TODAY)
    assert prov.source == "provider" and prov.value == pytest.approx(500.0 * 2.1e9)


def test_inconsistent_sec_share_counts_are_rejected():
    mc = resolve_market_cap(10.0, _m(shares_outstanding=(1e8, "2026-07-31"), shares_outstanding_bs=(1e5, "2026-06-30")),
                            None, None, None, None, None, TODAY)
    assert mc.value is None and "inconsistent_share_counts" in mc.flags


def test_screen_uses_resolved_market_cap_and_bank_revenue_components():
    fm = fundamental_metrics({"revenue": {"val": 5e9}, "net_income": {"val": 5e8}, "shares_outstanding": {"val": 1e8}},
                             price=50.0, market_cap=None)
    assert fm["market_cap"] is None and fm.get("earnings_yield") is None  # unknown stays unknown
    m = {"_bank_a": {"val": 40.0, "end": "2025-12-31"}, "_bank_b": {"val": 20.0, "end": "2025-12-31"}}
    combine_revenue_components(m)
    assert m["revenue"]["val"] == 60.0 and "net interest income" in m["revenue"]["concept"] and "_bank_a" not in m


def test_every_failing_eligibility_check_is_recorded():
    from app.config import Settings

    class Sec:
        security_type, cik, sic_code, sector, industry = "CS", "0000000001", None, None, None
    m = {"sessions": 60, "stale_sessions": 0, "revenue": 1e9, "net_income": None, "market_cap": None,
         "avg_dollar_volume_20d": 1e6, "market_cap_info": {"flags": [], "reason": "no shares"}}
    codes = eligibility_codes(Sec(), m, "sec_partial", 0.0, Settings())
    assert {"insufficient_financial_data", "unknown_market_cap", "insufficient_liquidity"} <= set(codes)


# ============================================================ discovery integration
def test_valuation_factor_respects_valuation_status():
    snap = {"base_return_12m": 0.4, "bear_return_12m": -0.1, "bull_return_12m": 0.8, "net_margin": 0.1}
    valid = factor_values({**snap, "valuation_status": "valid"}, "none_detected", None, 30)["valuation"]
    limited = factor_values({**snap, "valuation_status": "limited_data"}, "none_detected", None, 30)["valuation"]
    review = factor_values({**snap, "valuation_status": "requires_review"}, "none_detected", None, 30)["valuation"]
    assert valid == pytest.approx(1.0) and limited == pytest.approx(0.75) and review is None
    unknown_downside = factor_values({"base_return_12m": None, "bear_return_12m": None}, "none_detected", None, 30)
    assert unknown_downside["downside_risk"] is None  # missing valuation is not zero downside


def test_ranking_exclusion_keeps_only_supported_valuations():
    assert ranking_exclusion("valid", None) is None and ranking_exclusion("limited_data", None) is None
    assert ranking_exclusion(None, None) is None  # pre-guardrail analyses are judged by their targets
    v = {"guardrails": {"flags": [{"code": "unsupported_upside", "severity": "review", "message": "Base-case upside 230%"}]}}
    assert ranking_exclusion("requires_review", v).startswith("valuation requires review: Base-case upside")
    assert "unsupported" in ranking_exclusion("unsupported_valuation", None)
