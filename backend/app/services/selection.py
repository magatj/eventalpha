"""Bias-free candidate selection (pure, deterministic, unit-tested).

Every eligible universe stock is judged on baseline data collected for the WHOLE universe —
never on whether it happened to be analyzed before. Slots are filled through three pathways:

  baseline_leader (35)  best baseline score (financial strength + data freshness)
  new_catalyst    (15)  catalysts first detected since the previous discovery run
  underexplored   (10)  never analyzed, or analyzed longest ago

Pathways are filled in that order, skipping tickers already taken (deduplication); slots a
pathway cannot fill are refilled from the baseline ranking ("refill").

Ties are never broken alphabetically. Sort keys are data first; exact ties fall to a seeded hash
of (seed, ticker), so tied stocks rotate between runs while any single run is reproducible.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

PATHWAYS = ("baseline_leader", "new_catalyst", "underexplored")
DEFAULT_QUOTAS: dict[str, int] = {"baseline_leader": 35, "new_catalyst": 15, "underexplored": 10}
REFILL = "refill"
SCORE_DECIMALS = 4  # baseline scores equal to 4 decimals are treated as tied


@dataclass
class UniverseItem:
    ticker: str
    eligible: bool
    baseline_score: float | None = None
    freshness: float | None = None
    catalyst_status: str = "unknown"  # detected | none_detected | unknown
    catalyst_score: float | None = None
    catalyst_first_seen: datetime | None = None  # newest catalyst's detection (ingestion) time
    last_analyzed_at: datetime | None = None
    ineligible_reason: str | None = None
    company_id: int | None = None
    data_timestamps: dict = field(default_factory=dict)


@dataclass
class SelectionResult:
    selected: list[tuple[str, str]]  # (ticker, pathway that filled the slot), in selection order
    qualifying: dict[str, list[str]]  # ticker -> every pathway whose quota list it made
    tie_keys: dict[str, str]
    quotas: dict[str, int]
    filled: dict[str, int]  # slots actually filled per pathway (+ refill)

    @property
    def tickers(self) -> list[str]:
        return [t for t, _ in self.selected]

    def pathway_of(self, ticker: str) -> str | None:
        return next((p for t, p in self.selected if t == ticker), None)


def tie_key(seed: str, ticker: str) -> str:
    """Seeded pseudo-random rotation key; uncorrelated with alphabetical order."""
    return hashlib.sha256(f"{seed}|{ticker}".encode()).hexdigest()[:16]


def _r(x: float | None) -> float:
    return round(x, SCORE_DECIMALS) if x is not None else -1.0


def rank_baseline(items: Iterable[UniverseItem], seed: str) -> list[UniverseItem]:
    return sorted(items, key=lambda i: (-_r(i.baseline_score), -_r(i.freshness), tie_key(seed, i.ticker)))


def rank_new_catalysts(items: Iterable[UniverseItem], seed: str, new_since: datetime) -> list[UniverseItem]:
    fresh = [i for i in items if i.catalyst_status == "detected" and i.catalyst_first_seen
             and i.catalyst_first_seen >= new_since]
    return sorted(fresh, key=lambda i: (-_r(i.catalyst_score), -_r(i.baseline_score), tie_key(seed, i.ticker)))


def rank_underexplored(items: Iterable[UniverseItem], seed: str) -> list[UniverseItem]:
    # never analyzed first, then least recently analyzed; ties rotate by seed
    return sorted(items, key=lambda i: (i.last_analyzed_at is not None,
                                        i.last_analyzed_at.timestamp() if i.last_analyzed_at else 0.0,
                                        tie_key(seed, i.ticker)))


def select_candidates(items: list[UniverseItem], seed: str, new_since: datetime, total: int = 60,
                      quotas: dict[str, int] | None = None) -> SelectionResult:
    quotas = dict(quotas or DEFAULT_QUOTAS)
    eligible = [i for i in items if i.eligible]
    rankings = {
        "baseline_leader": rank_baseline(eligible, seed),
        "new_catalyst": rank_new_catalysts(eligible, seed, new_since),
        "underexplored": rank_underexplored(eligible, seed),
    }
    qualifying: dict[str, list[str]] = {}
    for p in PATHWAYS:
        for i in rankings[p][: quotas.get(p, 0)]:
            qualifying.setdefault(i.ticker, []).append(p)

    taken: set[str] = set()
    selected: list[tuple[str, str]] = []
    filled = {p: 0 for p in (*PATHWAYS, REFILL)}
    for p in PATHWAYS:
        for i in rankings[p]:
            if filled[p] >= quotas.get(p, 0) or len(selected) >= total:
                break
            if i.ticker in taken:
                continue  # deduplicate: already selected through an earlier pathway
            taken.add(i.ticker)
            selected.append((i.ticker, p))
            filled[p] += 1
    for i in rankings["baseline_leader"]:  # refill unused slots
        if len(selected) >= total:
            break
        if i.ticker not in taken:
            taken.add(i.ticker)
            selected.append((i.ticker, REFILL))
            filled[REFILL] += 1
    return SelectionResult(selected, qualifying, {i.ticker: tie_key(seed, i.ticker) for i in items}, quotas, filled)


def catalyst_status(has_catalyst: bool, news_checked_at: datetime | None, filings_checked_at: datetime | None,
                    now: datetime, max_age_hours: float = 48) -> str:
    """'detected' if any catalyst is linked; 'none_detected' only if BOTH news and filings were
    checked recently and found nothing; otherwise 'unknown' (absence of data is not absence of news)."""
    if has_catalyst:
        return "detected"

    def recent(ts: datetime | None) -> bool:
        return ts is not None and (now - ts).total_seconds() <= max_age_hours * 3600

    return "none_detected" if recent(news_checked_at) and recent(filings_checked_at) else "unknown"
