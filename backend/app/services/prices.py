"""Price storage, split adjustment and price statistics."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import CorporateAction, MarketDataRevision, StockPrice
from app.providers.market.base import CorporateActionData, PriceBar


def split_factor_after(d: date, splits: Iterable[CorporateActionData | CorporateAction]) -> float:
    """Cumulative price factor for a bar dated `d`: product of from/to for every split with ex_date > d."""
    f = 1.0
    for s in splits:
        if getattr(s, "action_type", "split") != "split":
            continue
        if s.split_from and s.split_to and s.ex_date > d:
            f *= s.split_from / s.split_to
    return f


def apply_split_adjustment(bars: list[PriceBar], splits: Iterable[CorporateActionData | CorporateAction]) -> list[PriceBar]:
    """Return split-adjusted copies of raw bars (prices scaled down, volume scaled up before each split)."""
    splits = [s for s in splits if getattr(s, "action_type", "split") == "split"]
    out = []
    for b in bars:
        f = split_factor_after(b.date, splits)
        if f == 1.0:
            out.append(b)
            continue
        out.append(b.model_copy(update={
            "open": b.open * f if b.open is not None else None,
            "high": b.high * f if b.high is not None else None,
            "low": b.low * f if b.low is not None else None,
            "close": b.close * f,
            "vwap": b.vwap * f if b.vwap is not None else None,
            "volume": b.volume / f if b.volume is not None else None,
        }))
    return out


def upsert_prices(session: Session, company_id: int, bars: list[PriceBar], source: str) -> int:
    """Insert raw bars; if a stored close changes (vendor correction), log the revision."""
    if not bars:
        return 0
    existing = {
        p.trade_date: p
        for p in session.scalars(
            select(StockPrice).where(
                StockPrice.company_id == company_id,
                StockPrice.trade_date.between(bars[0].date, bars[-1].date),
            )
        )
    }
    for b in bars:
        old = existing.get(b.date)
        if old is not None and not math.isclose(old.close, b.close, rel_tol=1e-6):
            session.add(MarketDataRevision(company_id=company_id, trade_date=b.date, field="close",
                                           old_value=old.close, new_value=b.close, source=source))
    rows = [
        dict(company_id=company_id, trade_date=b.date, open=b.open, high=b.high, low=b.low, close=b.close,
             volume=b.volume, vwap=b.vwap, source=source)
        for b in bars
    ]
    for i in range(0, len(rows), 1000):
        stmt = insert(StockPrice).values(rows[i:i + 1000])
        stmt = stmt.on_conflict_do_update(
            constraint="uq_price_company_date",
            set_={c: stmt.excluded[c] for c in ("open", "high", "low", "close", "volume", "vwap", "source")},
        )
        session.execute(stmt)
    return len(rows)


def upsert_corporate_actions(session: Session, company_id: int, actions: list[CorporateActionData], source: str) -> None:
    for a in actions:
        stmt = insert(CorporateAction).values(
            company_id=company_id, action_type=a.action_type, ex_date=a.ex_date, split_from=a.split_from,
            split_to=a.split_to, cash_amount=a.cash_amount, source=source,
        ).on_conflict_do_nothing(constraint="uq_corp_action")
        session.execute(stmt)


def load_adjusted_prices(session: Session, company_id: int, start: date | None = None,
                         end: date | None = None) -> pd.DataFrame:
    q = select(StockPrice).where(StockPrice.company_id == company_id)
    if start:
        q = q.where(StockPrice.trade_date >= start)
    if end:
        q = q.where(StockPrice.trade_date <= end)
    rows = list(session.scalars(q.order_by(StockPrice.trade_date)))
    splits = list(session.scalars(select(CorporateAction).where(
        CorporateAction.company_id == company_id, CorporateAction.action_type == "split")))
    bars = [PriceBar(date=r.trade_date, open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume,
                     vwap=r.vwap) for r in rows]
    adj = apply_split_adjustment(bars, splits)
    if not adj:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame([b.model_dump() for b in adj])
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


def annualized_volatility(closes: pd.Series, window: int = 252) -> float | None:
    r = np.log(closes.astype(float)).diff().dropna().tail(window)
    if len(r) < 20:
        return None
    return float(r.std(ddof=1) * math.sqrt(252))


def beta_vs(closes: pd.Series, bench: pd.Series, window: int = 504) -> float | None:
    """Beta from weekly log returns (weekly reduces asynchronous-trading noise)."""
    df = pd.concat([closes.rename("a"), bench.rename("b")], axis=1, sort=True).dropna().tail(window)
    if len(df) < 60:
        return None
    w = np.log(df.resample("W-FRI").last()).diff().dropna()
    if len(w) < 26 or w["b"].var() == 0:
        return None
    return float(w["a"].cov(w["b"]) / w["b"].var())


def max_drawdown(closes: pd.Series) -> float | None:
    if len(closes) < 2:
        return None
    peak = closes.cummax()
    return float((closes / peak - 1).min())


def price_stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    closes = df["close"]
    last_year = df[df.index >= df.index[-1] - pd.Timedelta(days=365)]
    vol = df.get("volume")
    adv = None
    if vol is not None and len(df) >= 5:
        adv = float((df["close"] * df["volume"]).tail(20).mean())
    return {
        "high_52w": float(last_year["high"].max()) if "high" in last_year and last_year["high"].notna().any() else float(last_year["close"].max()),
        "low_52w": float(last_year["low"].min()) if "low" in last_year and last_year["low"].notna().any() else float(last_year["close"].min()),
        "volatility_1y": annualized_volatility(closes),
        "max_drawdown_1y": max_drawdown(last_year["close"]),
        "avg_dollar_volume_20d": adv,
        "last_volume": float(df["volume"].iloc[-1]) if vol is not None and pd.notna(df["volume"].iloc[-1]) else None,
        "return_1y": float(closes.iloc[-1] / last_year["close"].iloc[0] - 1) if len(last_year) > 1 else None,
    }
