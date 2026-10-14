"""Company-specific historical valuation multiples and forecast-multiple selection.

Multiples are measured point-in-time: at each month-end the SEC facts *filed by that date* are
combined with that day's split-adjusted close (share and per-share facts are split-adjusted too, so
price and EPS are on the same share basis). Months whose denominator is negative, near zero or
anomalous (earnings far below the company's own normal margin) are rejected rather than producing
meaningless 80x-500x readings.

Forecast multiples (bear / base / bull) are chosen from evidence in this order:
  1. the company's own history (needs >= MIN_POINTS valid months)
  2. comparable companies (peer_comparison), as supporting evidence (weight PEER_WEIGHT when both exist)
  3. a fundamental anchor (e.g. justified P/TBV from ROE for banks), when supplied
  4. the broad sector reference band — only as a last resort, and the result is marked "limited"
The base multiple starts from today's multiple and moves only part-way toward the anchor over the
horizon (half-life REVERSION_HALF_LIFE_YEARS), so the base case does not assume an immediate re-rating.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app.services.financials import build_snapshot

MIN_POINTS = 6
PEER_WEIGHT = 0.3
FUNDAMENTAL_WEIGHT = 0.5  # share of the anchor when only peers (or nothing else) are available
FUNDAMENTAL_WEIGHT_WITH_HISTORY = 0.25  # share when the company's own history exists
SUPPORT_BAND = (0.8, 1.25)  # peers/fundamentals may move the anchor at most this far from the company's own median
REVERSION_HALF_LIFE_YEARS = 2.0
PE_BOUNDS = (3.0, 80.0)
MIN_MARGIN_FOR_PE = 0.02
DEPRESSED_FRACTION = 0.5  # TTM margin below this fraction of the company's median margin = depressed earnings


def earnings_state(net_margin: float | None, median_margin: float | None, recent_margins: list[float] | None = None) -> str:
    """normal | depressed | inflated — today's margin versus the company's own long-run median annual margin
    AND its last two fiscal years (a level sustained for two years is treated as structural, not a spike)."""
    if net_margin is None or median_margin is None or median_margin < 0.03:
        return "normal"
    recent = [x for x in (recent_margins or [])[-2:] if x is not None]
    recent_mean = sum(recent) / len(recent) if recent else None
    if net_margin < DEPRESSED_FRACTION * median_margin and (recent_mean is None or net_margin < 0.75 * recent_mean):
        return "depressed"
    if (net_margin > 1.5 * median_margin and net_margin - median_margin > 0.03
            and (recent_mean is None or net_margin > 1.25 * recent_mean)):
        return "inflated"
    return "normal"


@dataclass
class MultipleBand:
    kind: str  # pe | pb | ptbv | ps | pffo
    p10: float
    p25: float
    p50: float
    p75: float
    p90: float
    n: int
    rejected: int = 0
    source: str = "company_history"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def band_from(values: list[float], kind: str, rejected: int = 0, source: str = "company_history",
              detail: str = "") -> MultipleBand | None:
    vals = [v for v in values if v is not None and np.isfinite(v) and v > 0]
    if not vals:
        return None
    a = np.array(vals)
    p = [float(np.percentile(a, q)) for q in (10, 25, 50, 75, 90)]
    return MultipleBand(kind, *p, n=len(vals), rejected=rejected, source=source, detail=detail)


def historical_multiples(facts: list, prices: pd.DataFrame, splits: list[tuple[date, float]] | None = None,
                         months: int = 24, ffo_fn=None) -> dict[str, MultipleBand]:
    """Monthly point-in-time multiples over the available price history.

    Returns bands for P/E (valid earnings only), P/B, P/TBV and P/S, plus P/FFO when `ffo_fn(snapshot)`
    returns FFO per share. Rejections are counted per multiple."""
    if prices.empty:
        return {}
    month_ends = prices["close"].resample("ME").last().dropna().tail(months)
    pts: dict[str, list[float]] = {k: [] for k in ("pe", "pb", "ptbv", "ps", "pffo")}
    rejected = {k: 0 for k in pts}
    margins: list[tuple[float, float | None]] = []
    for ts, px in month_ends.items():
        snap = build_snapshot(facts, as_of=ts.date(), splits=splits)
        t = snap.ttm
        sh_out = t.get("shares_outstanding") or t.get("diluted_shares")
        eps, rev, nm = t.get("eps"), t.get("revenue"), snap.ratios.get("net_margin")
        if eps is not None and nm is not None:
            margins.append((px / eps if eps > 0 else -1.0, nm))
        else:
            rejected["pe"] += 1
        if sh_out and sh_out > 0:
            if rev and rev > 0:
                pts["ps"].append(px * sh_out / rev)
            if (t.get("book_value") or 0) > 0:
                pts["pb"].append(px * sh_out / t["book_value"])
            if (t.get("tangible_book_value") or 0) > 0:
                pts["ptbv"].append(px * sh_out / t["tangible_book_value"])
        if ffo_fn is not None:
            ffo = ffo_fn(snap)
            if ffo and ffo > 0:
                pts["pffo"].append(px / ffo)
            else:
                rejected["pffo"] += 1
    # P/E: reject negative / near-zero earnings and months whose margin is far below the company's norm
    valid_m = [m for _, m in margins if m is not None and m > 0]
    med_m = statistics.median(valid_m) if len(valid_m) >= 3 else None
    for pe, m in margins:
        if pe <= 0 or m is None or m < MIN_MARGIN_FOR_PE or (med_m and m < DEPRESSED_FRACTION * med_m):
            rejected["pe"] += 1
        elif PE_BOUNDS[0] <= pe <= PE_BOUNDS[1] * 1.5:
            pts["pe"].append(pe)
        else:
            rejected["pe"] += 1
    out = {}
    for k, vals in pts.items():
        b = band_from(vals, k, rejected[k], detail=f"{len(vals)} month-end observations, point-in-time SEC data")
        if b is not None:
            out[k] = b
    return out


@dataclass
class MultipleSelection:
    kind: str
    current: float | None
    bear: float
    base_anchor: float  # long-run central multiple the base case reverts toward
    bull: float
    low: float
    high: float
    basis: str  # company_history | company_and_peers | peers | fundamental | sector_reference
    limited: bool
    evidence: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def base_at(self, years: float) -> float:
        """Base-case multiple at the horizon: today's multiple (bounded by the evidence range) moving
        part-way toward the anchor (half-life REVERSION_HALF_LIFE_YEARS)."""
        if self.current is None:
            return self.base_anchor
        cur = min(max(self.current, self.low), self.high)
        w = 1 - 0.5 ** (years / REVERSION_HALF_LIFE_YEARS)
        return cur + (self.base_anchor - cur) * w

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in d.items()}


def _blend(parts: list[tuple[float, float]]) -> float:
    tw = sum(w for _, w in parts)
    return sum(v * w for v, w in parts) / tw


def select_multiples(kind: str, current: float | None, company: MultipleBand | None, peers: MultipleBand | None,
                     sector: tuple[float, float, float] | None, fundamental: float | None = None,
                     bounds: tuple[float, float] = PE_BOUNDS) -> MultipleSelection | None:
    """Bear / base / bull multiples from company history, peers, a fundamental anchor or (last resort)
    the sector reference band. Every input and the rule used are recorded in `evidence`."""
    lo_b, hi_b = bounds
    comp = company if company is not None and company.n >= MIN_POINTS else None
    peer = peers if peers is not None and peers.n >= 5 else None
    ev: dict[str, Any] = {"current": current, "company_history": company.to_dict() if company else None,
                          "peers": peers.to_dict() if peers else None, "fundamental_anchor": fundamental,
                          "sector_reference": list(sector) if sector else None}
    notes: list[str] = []
    if company is not None and comp is None:
        notes.append(f"Company history has only {company.n} valid observations (< {MIN_POINTS}); not used.")
    if comp is not None:
        anchors = [(comp.p50, 1.0)]
        lows, highs = [(comp.p25, 1.0)], [(comp.p75, 1.0)]
        basis = "company_history"
        if peer is not None:
            anchors.append((peer.p50, PEER_WEIGHT / (1 - PEER_WEIGHT)))
            lows.append((peer.p25, PEER_WEIGHT / (1 - PEER_WEIGHT)))
            highs.append((peer.p75, PEER_WEIGHT / (1 - PEER_WEIGHT)))
            basis = "company_and_peers"
        if fundamental is not None:
            fw = FUNDAMENTAL_WEIGHT_WITH_HISTORY
            anchors.append((fundamental, sum(w for _, w in anchors) * fw / (1 - fw)))
            basis += "+fundamental"
        anchor, low, high = _blend(anchors), _blend(lows), _blend(highs)
        # supporting evidence is bounded: it can nudge, not replace, the company's own valuation history
        bounded = min(max(anchor, comp.p50 * SUPPORT_BAND[0]), comp.p50 * SUPPORT_BAND[1])
        if bounded != anchor:
            notes.append(f"Peer/fundamental evidence would move the anchor to {anchor:.2f}x; limited to "
                         f"{bounded:.2f}x ({SUPPORT_BAND[0]:.0%}-{SUPPORT_BAND[1]:.0%} of the company's own median "
                         f"{comp.p50:.2f}x).")
            anchor = bounded
        low = min(max(low, comp.p25 * SUPPORT_BAND[0]), comp.p25 * SUPPORT_BAND[1])
        # never above what the company itself has traded at
        high = min(high, comp.p90, max(comp.p75 * SUPPORT_BAND[1], anchor))
        limited = False
    elif peer is not None:
        anchor, low, high = peer.p50, peer.p25, peer.p75
        basis, limited = "peers", False
        if fundamental is not None:
            anchor = FUNDAMENTAL_WEIGHT * fundamental + (1 - FUNDAMENTAL_WEIGHT) * anchor
            basis = "peers+fundamental"
        notes.append("No usable company valuation history; comparable companies used.")
    elif fundamental is not None:
        anchor = fundamental
        low, high = fundamental * 0.8, fundamental * 1.2
        basis, limited = "fundamental", True
        notes.append("No company history or peers; fundamental (justified) multiple used with a +/-20% range.")
    elif sector is not None:
        low, anchor, high = sector
        basis, limited = "sector_reference", True
        notes.append("No company history, peers or fundamental anchor: broad sector reference band used "
                     "(model assumption; analysis marked limited).")
    else:
        return None
    low, anchor, high = (min(max(x, lo_b), hi_b) for x in (low, anchor, high))
    low, high = min(low, anchor), max(high, anchor)
    # Bear never assumes expansion from today's multiple; bull never assumes more than the evidence's high.
    bear = min(low, current) if current is not None else low
    bear = max(bear, lo_b)
    bull = high
    return MultipleSelection(kind, current, bear, anchor, bull, low, high, basis, limited, ev, notes)
