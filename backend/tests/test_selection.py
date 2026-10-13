"""Candidate selection: no feedback loop, no alphabetical bias, explicit pathways."""

import random
import string
from datetime import UTC, datetime, timedelta

import pytest

from app.services.selection import (
    DEFAULT_QUOTAS,
    REFILL,
    UniverseItem,
    catalyst_status,
    select_candidates,
    tie_key,
)

NOW = datetime(2026, 10, 13, 22, 0, tzinfo=UTC)
NEW_SINCE = NOW - timedelta(days=1)


def tickers(n: int, seed: int = 1) -> list[str]:
    rng = random.Random(seed)
    out: set[str] = set()
    while len(out) < n:
        out.add("".join(rng.choice(string.ascii_uppercase) for _ in range(rng.randint(2, 4))))
    return sorted(out)


def item(t: str, score: float = 0.5, analyzed: datetime | None = NOW - timedelta(days=1), **kw) -> UniverseItem:
    return UniverseItem(ticker=t, eligible=kw.pop("eligible", True), baseline_score=score, freshness=1.0,
                        last_analyzed_at=analyzed, **kw)


def universe(n=116, unseen=0, seed=1) -> list[UniverseItem]:
    names = tickers(n, seed)
    rng = random.Random(seed)
    return [item(t, score=rng.random(), analyzed=None if i < unseen else NOW - timedelta(days=rng.randint(0, 5)))
            for i, t in enumerate(names)]


# --------------------------------------------------------------- required: unseen stocks get in


def test_never_analyzed_stocks_enter_top_60_even_with_weak_baselines():
    """The feedback loop is gone: 16 never-analyzed stocks with the WORST baselines and no
    catalysts still get underexplored slots."""
    analyzed = [item(t, score=0.6 + i / 1000) for i, t in enumerate(tickers(100, seed=2))]
    unseen = [item(t, score=0.01, analyzed=None) for t in tickers(16, seed=3) if t not in {i.ticker for i in analyzed}]
    sel = select_candidates(analyzed + unseen, "2026-10-13", NEW_SINCE)
    picked = {t for t, p in sel.selected if p == "underexplored"}
    assert len(sel.selected) == 60
    assert len(picked) == DEFAULT_QUOTAS["underexplored"]
    assert picked <= {u.ticker for u in unseen}


def test_never_analyzed_stock_with_strong_fundamentals_is_a_baseline_leader():
    items = [item(t, score=0.5) for t in tickers(100, seed=4)] + [item("ZZZZ", score=0.99, analyzed=None)]
    sel = select_candidates(items, "s", NEW_SINCE)
    assert sel.selected[0] == ("ZZZZ", "baseline_leader")


def test_new_catalyst_pathway_admits_low_baseline_stocks():
    items = [item(t, score=0.9) for t in tickers(100, seed=5)]
    items += [item(f"Q{i}", score=0.05, catalyst_status="detected", catalyst_score=0.8,
                   catalyst_first_seen=NOW - timedelta(hours=2)) for i in range(20)]
    sel = select_candidates(items, "s", NEW_SINCE)
    cats = [t for t, p in sel.selected if p == "new_catalyst"]
    assert len(cats) == 15 and all(t.startswith("Q") for t in cats)


def test_old_catalysts_do_not_use_the_new_catalyst_pathway():
    items = [item(t) for t in tickers(80, seed=6)]
    items.append(item("OLDC", score=0.0, catalyst_status="detected", catalyst_score=0.9,
                      catalyst_first_seen=NEW_SINCE - timedelta(days=3)))
    sel = select_candidates(items, "s", NEW_SINCE)
    assert sel.pathway_of("OLDC") != "new_catalyst"


# --------------------------------------------------------------- required: alphabet is irrelevant


def test_alphabetical_order_does_not_drive_selection_when_all_tied():
    """All stocks identical: across many seeds, selection frequency must be uncorrelated with
    alphabetical rank (the old engine picked exactly the 60 alphabetically-last tickers)."""
    names = tickers(116, seed=7)
    items = [item(t, score=0.5, analyzed=NOW - timedelta(days=1)) for t in names]
    freq = {t: 0 for t in names}
    seeds = [f"day-{k}" for k in range(300)]
    for sd in seeds:
        for t in select_candidates(items, sd, NEW_SINCE).tickers:
            freq[t] += 1
    alpha_rank = {t: i for i, t in enumerate(sorted(names))}
    xs = [alpha_rank[t] for t in names]
    ys = [freq[t] for t in names]
    rx = _ranks(xs)
    ry = _ranks(ys)
    rho = _pearson(rx, ry)
    assert abs(rho) < 0.2, rho
    # every stock gets a turn, and roughly its fair share (60/116 ≈ 52%)
    assert min(ys) > 0
    assert 0.40 < sum(ys) / (len(seeds) * len(names)) < 0.60
    one = set(select_candidates(items, "day-0", NEW_SINCE).tickers)
    assert one != set(sorted(names)[:60]) and one != set(sorted(names)[-60:])


def test_renaming_tickers_does_not_change_which_data_gets_selected():
    """Distinct baseline scores: reversing the alphabet of every ticker must select the same
    underlying stocks (selection follows the data, not the name)."""
    names = tickers(116, seed=8)
    rng = random.Random(8)
    scores = {t: rng.random() for t in names}
    swap = str.maketrans(string.ascii_uppercase, string.ascii_uppercase[::-1])
    a = [item(t, score=scores[t]) for t in names]
    b = [item(t.translate(swap), score=scores[t]) for t in names]
    sel_a = {t for t, p in select_candidates(a, "s", NEW_SINCE).selected if p == "baseline_leader"}
    sel_b = {t for t, p in select_candidates(b, "s", NEW_SINCE).selected if p == "baseline_leader"}
    assert {t.translate(swap) for t in sel_a} == sel_b


def test_selection_is_independent_of_input_order():
    items = universe(116, unseen=20, seed=9)
    shuffled = items[:]
    random.Random(1).shuffle(shuffled)
    assert select_candidates(items, "s", NEW_SINCE).selected == select_candidates(shuffled, "s", NEW_SINCE).selected


def test_seeded_rotation_is_reproducible_and_rotates():
    items = [item(t, score=0.5) for t in tickers(116, seed=10)]
    a1 = select_candidates(items, "2026-10-13", NEW_SINCE).tickers
    a2 = select_candidates(items, "2026-10-13", NEW_SINCE).tickers
    b = select_candidates(items, "2026-10-14", NEW_SINCE).tickers
    assert a1 == a2 and set(a1) != set(b)
    assert tie_key("x", "AAPL") != tie_key("y", "AAPL")


# --------------------------------------------------------------- quotas, dedupe, refill, eligibility


def test_quotas_dedupe_and_refill():
    items = [item(t, score=0.5 + i / 1000, analyzed=None if i < 5 else NOW) for i, t in enumerate(tickers(116, seed=11))]
    # only 4 new-catalyst stocks exist -> 11 catalyst slots must be refilled
    for it in items[:4]:
        it.catalyst_status, it.catalyst_score, it.catalyst_first_seen = "detected", 0.7, NOW
    sel = select_candidates(items, "s", NEW_SINCE)
    assert len(sel.selected) == 60 and len(set(sel.tickers)) == 60
    assert sel.filled["baseline_leader"] == 35
    assert sel.filled["new_catalyst"] == 4
    assert sel.filled["underexplored"] == 10
    assert sel.filled[REFILL] == 11
    multi = [t for t, ps in sel.qualifying.items() if len(ps) > 1]
    assert all(sel.tickers.count(t) == 1 for t in multi)


def test_ineligible_stocks_are_never_selected():
    items = universe(116, unseen=10, seed=12)
    for it in items[::3]:
        it.eligible, it.ineligible_reason = False, "illiquid"
    sel = select_candidates(items, "s", NEW_SINCE)
    bad = {i.ticker for i in items if not i.eligible}
    assert not bad & set(sel.tickers)


def test_small_universe_selects_all_eligible():
    items = universe(22, unseen=3, seed=13)
    assert len(select_candidates(items, "s", NEW_SINCE).selected) == 22


# --------------------------------------------------------------- coverage vs materiality


@pytest.mark.parametrize("has, news_h, filings_h, expected", [
    (True, None, None, "detected"),
    (False, 2, 3, "none_detected"),       # checked recently, nothing found -> genuinely none
    (False, None, 3, "unknown"),           # news never checked -> unknown, not "none"
    (False, 100, 3, "unknown"),            # stale check -> unknown
    (False, 2, None, "unknown"),
])
def test_catalyst_coverage_classification(has, news_h, filings_h, expected):
    ts = lambda h: None if h is None else NOW - timedelta(hours=h)  # noqa: E731
    assert catalyst_status(has, ts(news_h), ts(filings_h), NOW) == expected


def _ranks(v: list[float]) -> list[float]:
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def _pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    va = sum((x - ma) ** 2 for x in a) ** 0.5
    vb = sum((y - mb) ** 2 for y in b) ** 0.5
    return cov / (va * vb)
