"""The six-factor score: explicit formula, bounds, penalties, unknown-vs-zero."""

import pytest

from app.services.discovery_scoring import (
    PENALTY_WEIGHTS,
    POSITIVE_WEIGHTS,
    band_for,
    compute_score,
    factor_values,
)

ALL = list(POSITIVE_WEIGHTS) + list(PENALTY_WEIGHTS)


def factors(pos: float, pen: float, **override):
    f = {k: pos for k in POSITIVE_WEIGHTS} | {k: pen for k in PENALTY_WEIGHTS}
    f.update(override)
    return f


def test_best_possible_is_100_and_worst_is_0():
    assert compute_score(factors(1.0, 0.0)).exact == pytest.approx(100.0)
    assert compute_score(factors(0.0, 1.0)).exact == pytest.approx(0.0)


def test_formula_worked_example():
    f = {"financial_strength": 0.8, "catalyst_materiality": 0.5, "valuation": 0.6, "data_freshness": 1.0,
         "forecast_uncertainty": 0.4, "downside_risk": 0.3}
    raw = 0.25 * 0.8 + 0.20 * 0.5 + 0.25 * 0.6 + 0.10 * 1.0 - 0.10 * 0.4 - 0.10 * 0.3  # 0.48
    best, worst = 0.80, -0.20
    r = compute_score(f)
    assert r.raw == pytest.approx(raw)
    assert r.exact == pytest.approx(100 * (raw - worst) / (best - worst))  # 68.0
    assert r.score == 70 and r.band == "Strong"
    assert sum(r.contributions.values()) == pytest.approx(r.exact - 100 * (-worst) / (best - worst) + 0, abs=0.05)


def test_penalties_reduce_score():
    lo = compute_score(factors(0.7, 0.0)).exact
    hi_pen = compute_score(factors(0.7, 1.0)).exact
    assert hi_pen < lo
    assert compute_score(factors(0.7, 1.0)).contributions["downside_risk"] < 0


def test_unknown_catalyst_is_not_scored_as_zero():
    f_unknown = factors(0.8, 0.2, catalyst_materiality=None)
    f_zero = factors(0.8, 0.2, catalyst_materiality=0.0)
    u, z = compute_score(f_unknown), compute_score(f_zero)
    assert "catalyst_materiality" in u.unknown_factors
    assert u.exact > z.exact  # a missing check is not evidence of no catalyst
    # renormalization: identical to the same stock scored without that factor's weight
    assert u.best == pytest.approx(sum(POSITIVE_WEIGHTS.values()) - POSITIVE_WEIGHTS["catalyst_materiality"])


def test_factor_values_map_catalyst_status():
    snap = {"net_margin": 0.2, "base_return_12m": 0.1, "bear_return_12m": -0.2, "bull_return_12m": 0.4}
    assert factor_values(snap, "unknown", None, 30)["catalyst_materiality"] is None
    assert factor_values(snap, "none_detected", None, 30)["catalyst_materiality"] == 0.0
    assert factor_values(snap, "detected", 0.7, 30)["catalyst_materiality"] == 0.7


def test_score_rounds_to_5_and_bands():
    for x in (0, 3, 49.9, 50, 69, 70, 100):
        assert band_for(x) == ("Strong" if x >= 70 else "Moderate" if x >= 50 else "Weak")
    r = compute_score(factors(0.53, 0.31))
    assert r.score % 5 == 0 and abs(r.score - r.exact) <= 2.5


def test_no_known_factors_is_unscored():
    r = compute_score({k: None for k in ALL})
    assert r.score is None and r.band == "Unscored"
