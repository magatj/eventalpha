from datetime import date

import pytest

from app.services.forecasting.engine import (
    AIAdjustment,
    ForecastEngine,
    ForecastInputs,
    cost_of_equity,
    dcf_valuation,
    eps_from,
    faded_growth_path,
    forward_pe_target,
    project_revenue_faded,
    projected_revenue,
    run_all_horizons,
    wacc,
)
from app.services.forecasting.sectors import SECTORS, profile, sector_from_sic


def inputs(**kw) -> ForecastInputs:
    base = dict(
        ticker="TEST", price=100.0, price_date=date(2026, 10, 9), sector=profile("Technology"),
        revenue_ttm=10e9, revenue_growth_yoy=0.12, revenue_cagr_3y=0.10, net_margin=0.20, operating_margin=0.25,
        fcf_margin=0.18, eps_ttm=4.0, shares=0.5e9, share_change_rate=-0.01, cash=3e9, debt=1e9,
        margin_history=[{"revenue": r, "net_margin": m, "fcf_margin": m - 0.02}
                        for r, m in ((7e9, 0.18), (8e9, 0.19), (9e9, 0.21), (10e9, 0.20))],
        beta=1.1, volatility=0.30, historical_pe_band=(20.0, 25.0, 30.0),
    )
    base.update(kw)
    return ForecastInputs(**base)


# ---------------------------------------------------------------- formulas


def test_projected_revenue_formula():
    assert projected_revenue(100, 0.10, 2) == pytest.approx(121.0)


def test_growth_fades_toward_long_run():
    path = faded_growth_path(0.40, 5, long_run=0.04, fade=0.75)
    assert path[0] == pytest.approx(0.40) and path[1] == pytest.approx(0.04 + 0.36 * 0.75)
    assert all(a > b for a, b in zip(path, path[1:], strict=False))
    assert project_revenue_faded(100, 0.04, 2.5) == pytest.approx(100 * 1.04**2.5)


def test_eps_and_forward_pe():
    assert eps_from(10e9, 0.2, 0.5e9) == pytest.approx(4.0)
    assert forward_pe_target(4.0, 25) == 100.0
    assert forward_pe_target(-1.0, 25) is None  # negative earnings -> P/E not applicable
    assert forward_pe_target(None, 25) is None


def test_dcf_known_value():
    r = dcf_valuation([100.0] * 5, 0.10, 0.02, cash=50, debt=150, shares=10)
    pv = sum(100 / 1.1**k for k in range(1, 6))
    tv = 100 * 1.02 / 0.08
    assert r.enterprise_value == pytest.approx(pv + tv / 1.1**5)
    assert r.per_share == pytest.approx((r.enterprise_value - 100) / 10)
    with pytest.raises(ValueError):
        dcf_valuation([1.0], 0.03, 0.03, 0, 0, 1)


def test_wacc_and_capm():
    assert cost_of_equity(0.04, 1.2, 0.05) == pytest.approx(0.10)
    assert wacc(800, 200, 0.10, 0.05, 0.2) == pytest.approx(0.8 * 0.10 + 0.2 * 0.04)
    assert wacc(0, 0, 0.1, 0.05, 0.2) == 0.1


# ---------------------------------------------------------------- scenarios


def test_scenarios_are_ordered_and_complete():
    for h in run_all_horizons(inputs()):
        b, m, u = (h.scenarios[s] for s in ("bear", "base", "bull"))
        assert b.target_price < m.target_price < u.target_price
        assert b.revenue < m.revenue < u.revenue
        for s in (b, m, u):
            assert s.eps and s.revenue_growth_annual is not None and s.potential_return is not None
            assert s.explanation and s.free_cash_flow
        assert h.target_date > h.observed_price_date
        assert "not predictions" in h.methods["interpretation"]


def test_target_dates_for_all_horizons():
    hs = {h.horizon_months: h.target_date for h in run_all_horizons(inputs())}
    assert hs == {3: date(2027, 1, 9), 6: date(2027, 4, 9), 12: date(2027, 10, 9), 24: date(2028, 10, 9),
                  36: date(2029, 10, 9), 60: date(2031, 10, 9)}


def test_blend_weights_pe_and_dcf():
    s = ForecastEngine(inputs()).run(12).scenarios["base"]
    assert s.target_price == pytest.approx(0.6 * s.target_price_pe + 0.4 * s.target_price_dcf)


def test_negative_earnings_fall_back_to_ev_sales():
    h = ForecastEngine(inputs(net_margin=-0.10, eps_ttm=-2.0, fcf_margin=-0.05,
                              margin_history=[], historical_pe_band=None)).run(12)
    base = h.scenarios["base"]
    assert base.target_price_pe is None and base.target_price_dcf is None
    assert base.target_price_ev_sales and base.target_price == base.target_price_ev_sales
    assert h.methods["dcf"]["applicable"] is False


def test_marginal_profitability_uses_one_regime_for_all_scenarios():
    """Barely-profitable bull vs loss-making bear must not switch methods (P/E on tiny EPS)."""
    h = ForecastEngine(inputs(net_margin=-0.007, eps_ttm=-0.3, fcf_margin=-0.02, historical_pe_band=None,
                              margin_history=[{"revenue": 1e9, "net_margin": m, "fcf_margin": -0.02} for m in (-0.01, 0.002)])).run(12)
    assert h.methods["regime"] == "sales"
    assert all(s.target_price_pe is None for s in h.scenarios.values())
    t = [h.scenarios[s].target_price for s in ("bear", "base", "bull")]
    assert t[0] < t[1] < t[2]


def test_financials_sector_skips_dcf():
    h = ForecastEngine(inputs(sector=SECTORS["Financials"])).run(12)
    assert h.methods["dcf"]["applicable"] is False
    assert h.scenarios["base"].target_price_dcf is None and h.scenarios["base"].target_price_pe


def test_non_comparable_currency_disables_per_share_targets():
    h = ForecastEngine(inputs(currency_ok=False)).run(12)
    assert all(s.target_price is None for s in h.scenarios.values())


def test_missing_revenue_is_reported_not_invented():
    h = ForecastEngine(inputs(revenue_ttm=None)).run(12)
    assert all(s.target_price is None for s in h.scenarios.values())
    assert "Insufficient" in h.scenarios["base"].explanation


def test_distorted_company_pe_history_is_ignored():
    eng = ForecastEngine(inputs(historical_pe_band=(400.0, 900.0, 1500.0)))
    assert eng.pe_band == (18, 27, 38)
    assert any("distorted" in w for w in eng.warnings)


def test_base_multiple_stays_within_band():
    eng = ForecastEngine(inputs(price=400.0))  # current P/E 100x, far above band
    lo, _, hi = eng.pe_band
    for years in (0.25, 1, 5):
        assert lo <= eng.scenario_pe("base", years) <= hi


def test_ai_adjustments_are_bounded_and_recorded():
    adj = AIAdjustment("base", revenue_growth_pp=0.50, net_margin_pp=-0.5, rationale="new contract", source_labels=["S3"])
    plain = ForecastEngine(inputs())
    eng = ForecastEngine(inputs(ai_adjustments=[adj]))
    assert eng.scenario_growth("base") == pytest.approx(plain.scenario_growth("base") + 0.05)
    assert eng.scenario_margin("base") == pytest.approx(plain.scenario_margin("base") - 0.03)
    assert any(a.kind == "ai_adjustment" and "S3" in a.basis for a in eng.assumptions)


def test_terminal_growth_below_discount_rate():
    eng = ForecastEngine(inputs(beta=0.6, debt=0))
    for sc in ("bear", "base", "bull"):
        assert eng.terminal_growth(sc) <= eng.wacc - 0.03


def test_unstable_fcf_reduces_dcf_weight():
    hist = [{"revenue": 1e9, "net_margin": 0.1, "fcf_margin": m} for m in (-0.1, 0.3, 0.05, 0.2)]
    s = ForecastEngine(inputs(margin_history=hist)).run(12).scenarios["base"]
    assert s.details["method_weights"]["dcf"] == pytest.approx(0.25 / 0.85, rel=1e-3)


def test_sensitivity_grids_and_volatility_band():
    h = ForecastEngine(inputs()).run(12)
    g = h.sensitivity["pe_growth"]
    assert len(g["target_prices"]) == 5 and all(len(r) == 5 for r in g["target_prices"])
    row = g["target_prices"][2]
    assert row == sorted(row)  # higher multiple -> higher price
    assert "dcf_wacc_terminal" in h.sensitivity
    v = h.volatility_range
    assert v["low_1sigma"] < 100 < v["high_1sigma"] and "not a forecast" in v["note"]


def test_no_probabilities_assigned():
    h = ForecastEngine(inputs()).run(12)
    assert "No probabilities" in h.methods["interpretation"]
    assert not any("probab" in k for s in h.scenarios.values() for k in s.details)


@pytest.mark.parametrize("sic,expected", [
    ("3674", "Technology"),
    ("7372", "Technology"), ("6021", "Financials"), ("6798", "Real Estate"), ("2834", "Health Care"),
    ("3021", "Consumer Discretionary"), ("2911", "Energy"), ("4911", "Utilities"), (None, "Industrials"),
])
def test_sector_mapping(sic, expected):
    assert sector_from_sic(sic) == expected


def test_semiconductor_description_maps_to_technology():
    assert sector_from_sic("3674", "Semiconductors & Related Devices") == "Technology"


def test_implausible_valuation_is_withheld_not_shown():
    """Bad source units (e.g. shares in millions) must never surface as a $169M target."""
    h = ForecastEngine(inputs(shares=500.0, eps_ttm=4e6)).run(12)
    assert all(s.target_price is None and s.potential_return is None for s in h.scenarios.values())
    assert any("Implausible valuation" in w for w in h.warnings)
    assert h.scenarios["base"].details["withheld_target"] > 1e6
