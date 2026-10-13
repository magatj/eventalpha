from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.providers.market.base import CorporateActionData, PriceBar
from app.services.prices import (
    annualized_volatility,
    apply_split_adjustment,
    beta_vs,
    max_drawdown,
    split_factor_after,
)


def bar(d, c, v=100.0):
    return PriceBar(date=d, open=c, high=c, low=c, close=c, volume=v)


def test_multiple_splits_compound():
    splits = [CorporateActionData(action_type="split", ex_date=date(2021, 7, 20), split_from=1, split_to=4),
              CorporateActionData(action_type="split", ex_date=date(2024, 6, 10), split_from=1, split_to=10)]
    assert split_factor_after(date(2021, 7, 19), splits) == pytest.approx(1 / 40)
    assert split_factor_after(date(2024, 6, 7), splits) == pytest.approx(1 / 10)
    assert split_factor_after(date(2024, 6, 10), splits) == 1.0


def test_reverse_split_and_dividends_ignored():
    actions = [CorporateActionData(action_type="split", ex_date=date(2024, 1, 2), split_from=10, split_to=1),
               CorporateActionData(action_type="dividend", ex_date=date(2024, 1, 2), cash_amount=0.5)]
    out = apply_split_adjustment([bar(date(2023, 12, 29), 1.0, 1000)], actions)
    assert out[0].close == pytest.approx(10.0) and out[0].volume == pytest.approx(100.0)


def test_volatility_and_beta():
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2024-01-01", periods=400)
    m = pd.Series(np.exp(np.cumsum(rng.normal(0, 0.01, 400))), index=idx)
    s = pd.Series(np.exp(np.cumsum(2 * np.log(m).diff().fillna(0).values)), index=idx)  # 2x leveraged
    assert beta_vs(s, m) == pytest.approx(2.0, rel=0.05)
    assert annualized_volatility(m) == pytest.approx(0.01 * np.sqrt(252), rel=0.15)
    assert annualized_volatility(m.head(5)) is None


def test_max_drawdown():
    assert max_drawdown(pd.Series([100, 120, 60, 90])) == pytest.approx(-0.5)
