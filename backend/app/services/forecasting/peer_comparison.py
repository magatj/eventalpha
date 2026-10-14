"""Comparable-company multiples from the stored market-wide screen (no extra API calls).

Peers are chosen by industry first (same 4-digit SIC, widening to 3 then 2 digits only when too few),
then ranked by similarity in size (log market cap), profitability (net margin) and growth. Peer
multiples are *supporting evidence* for multiple selection; a company is never forced to the peer
median (see company_multiples.select_multiples).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Security
from app.services.forecasting.company_multiples import MultipleBand, band_from

MIN_PEERS = 5
MAX_PEERS = 15
SIZE_RANGE = (0.1, 10.0)  # peer market cap within 0.1x-10x of the company's


@dataclass
class PeerSet:
    level: str  # e.g. "SIC 3674" | "SIC 367x" | "SIC 36xx"
    tickers: list[str]
    bands: dict[str, MultipleBand] = field(default_factory=dict)
    profitable_margin: float | None = None  # median net margin of the profitable peers

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "tickers": self.tickers, "bands": {k: b.to_dict() for k, b in self.bands.items()},
                "profitable_margin": self.profitable_margin}


def _metrics(sec: Security) -> dict[str, Any] | None:
    m = sec.screen_metrics or {}
    mc = sec.market_cap
    if not mc or mc <= 0:
        return None
    ni, rev, eq = m.get("net_income"), m.get("revenue"), m.get("equity")
    return {
        "ticker": sec.ticker, "mcap": mc, "sic": sec.sic_code or "",
        "pe": mc / ni if ni and ni > 0 and rev and ni / rev >= 0.02 else None,
        "pb": mc / eq if eq and eq > 0 else None,
        "ps": mc / rev if rev and rev > 0 else None,
        "margin": ni / rev if ni is not None and rev and rev > 0 else None,
        "growth": m.get("revenue_growth_yoy"),
    }


def _distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    d = abs(math.log(a["mcap"] / b["mcap"]))
    if a["margin"] is not None and b["margin"] is not None:
        d += 4 * abs(a["margin"] - b["margin"])
    if a["growth"] is not None and b["growth"] is not None:
        d += 2 * abs(max(min(a["growth"], 1), -1) - max(min(b["growth"], 1), -1))
    return d


def find_peers(session: Session, mode: str, ticker: str, sic: str | None, market_cap: float | None,
               net_margin: float | None = None, growth: float | None = None) -> PeerSet | None:
    if not sic or not market_cap:
        return None
    me = {"mcap": market_cap, "margin": net_margin, "growth": growth}
    rows = session.scalars(select(Security).where(
        Security.data_mode == mode, Security.active, Security.ticker != ticker,
        Security.sic_code.is_not(None), Security.market_cap.is_not(None),
        Security.sic_code.like(sic[:2] + "%"))).all()
    cands = [m for m in (_metrics(r) for r in rows) if m and SIZE_RANGE[0] <= m["mcap"] / market_cap <= SIZE_RANGE[1]]
    for digits, label in ((4, f"SIC {sic[:4]}"), (3, f"SIC {sic[:3]}x"), (2, f"SIC {sic[:2]}xx")):
        group = [c for c in cands if c["sic"][:digits] == sic[:digits]]
        if len(group) >= MIN_PEERS:
            group.sort(key=lambda c: (_distance(me, c), c["ticker"]))
            chosen = group[:MAX_PEERS]
            bands = {}
            for k in ("pe", "pb", "ps"):
                b = band_from([c[k] for c in chosen if c[k] is not None], k, source="peers", detail=label)
                if b is not None and b.n >= MIN_PEERS:
                    bands[k] = b
            margins = sorted(c["margin"] for c in chosen if c["margin"] is not None and c["margin"] > 0)
            pm = margins[len(margins) // 2] if len(margins) >= 3 else None
            return PeerSet(label, [c["ticker"] for c in chosen], bands, pm)
    return None
