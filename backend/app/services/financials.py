"""Persist normalized SEC facts and build point-in-time financial snapshots."""

from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import FinancialStatement
from app.providers.sec.xbrl import FinancialFact


def upsert_facts(session: Session, company_id: int, facts: list[FinancialFact], source: str = "sec_edgar") -> int:
    rows = [
        dict(company_id=company_id, metric=f.metric, period_type=f.period_type, fiscal_year=f.fiscal_year,
             fiscal_period=f.fiscal_period, period_start=f.period_start, period_end=f.period_end, value=f.value,
             unit=f.unit, concept=f.concept, taxonomy=f.taxonomy, accession=f.accession, form=f.form,
             filed_date=f.filed_date, derived=f.derived, derivation=f.derivation, source=source)
        for f in facts
    ]
    # NULL period_start would defeat the unique constraint for instants; use period_end as a sentinel.
    for r in rows:
        if r["period_start"] is None:
            r["period_start"] = r["period_end"]
    seen: set[tuple] = set()
    unique_rows = []
    for r in rows:
        k = (r["metric"], r["period_type"], r["period_start"], r["period_end"], r["accession"])
        if k not in seen:
            seen.add(k)
            unique_rows.append(r)
    for i in range(0, len(unique_rows), 1000):
        stmt = insert(FinancialStatement).values(unique_rows[i:i + 1000]).on_conflict_do_nothing(constraint="uq_fin_fact")
        session.execute(stmt)
    return len(unique_rows)


def load_facts(session: Session, company_id: int) -> list[FinancialFact]:
    rows = session.scalars(select(FinancialStatement).where(FinancialStatement.company_id == company_id))
    return [
        FinancialFact(
            metric=r.metric, period_type=r.period_type,
            period_start=None if r.period_type == "instant" else r.period_start, period_end=r.period_end,
            value=float(r.value), unit=r.unit, concept=r.concept, taxonomy=r.taxonomy, accession=r.accession,
            form=r.form, filed_date=r.filed_date, fiscal_year=r.fiscal_year, fiscal_period=r.fiscal_period,
            derived=r.derived, derivation=r.derivation,
        )
        for r in rows
    ]


@dataclass
class PeriodValue:
    period_start: date | None
    period_end: date
    value: float
    fiscal_year: int | None
    fiscal_period: str | None
    accession: str
    filed_date: date
    derived: bool
    concept: str


@dataclass
class FinancialSnapshot:
    as_of: date
    currency: str
    financial_data_version: str | None
    latest_filed: date | None
    quarterly: dict[str, list[PeriodValue]] = field(default_factory=dict)
    annual: dict[str, list[PeriodValue]] = field(default_factory=dict)
    instant: dict[str, PeriodValue] = field(default_factory=dict)
    ttm: dict[str, float | None] = field(default_factory=dict)
    ttm_prior: dict[str, float | None] = field(default_factory=dict)
    ttm_period_end: date | None = None
    ratios: dict[str, float | None] = field(default_factory=dict)
    margin_history: list[dict[str, Any]] = field(default_factory=list)
    foreign_issuer: bool = False  # files 20-F/40-F: per-share data is per ordinary share, not per ADR
    warnings: list[str] = field(default_factory=list)
    accessions: list[str] = field(default_factory=list)
    instant_history: dict[str, list[PeriodValue]] = field(default_factory=dict)
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)  # metric -> SEC concept/period/filing
    validation: list[str] = field(default_factory=list)  # inconsistencies found (data kept, flagged)
    split_adjustments: list[str] = field(default_factory=list)
    financial_status: str = "valid"  # valid | data_unavailable | invalid | unsupported_accounting

    @property
    def has_data(self) -> bool:
        return bool(self.annual.get("revenue") or self.quarterly.get("revenue"))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def _dedupe_latest(facts: list[FinancialFact]) -> list[PeriodValue]:
    """One value per period: the latest-filed version (restatements win)."""
    best: dict[tuple, FinancialFact] = {}
    for f in facts:
        k = (f.period_end,) if f.period_type == "instant" else (f.period_start, f.period_end)
        cur = best.get(k)
        if cur is None or (f.filed_date, not f.derived) > (cur.filed_date, not cur.derived):
            best[k] = f
    # collapse periods sharing an end date (52/53-week calendars): keep duration closest to target
    by_end: dict[date, FinancialFact] = {}
    for f in best.values():
        cur = by_end.get(f.period_end)
        if cur is None:
            by_end[f.period_end] = f
            continue
        target = 91 if f.period_type == "quarterly" else 365
        if f.period_start and cur.period_start:
            if abs((f.period_end - f.period_start).days - target) < abs((cur.period_end - cur.period_start).days - target):
                by_end[f.period_end] = f
    return [
        PeriodValue(f.period_start, f.period_end, f.value, f.fiscal_year, f.fiscal_period, f.accession,
                    f.filed_date, f.derived, f.concept)
        for f in sorted(by_end.values(), key=lambda x: x.period_end)
    ]


def _sum_last4(series: list[PeriodValue], offset: int = 0) -> tuple[float | None, date | None]:
    """Trailing-twelve-month sum of 4 contiguous quarters ending `offset` quarters ago."""
    if len(series) < 4 + offset:
        return None, None
    window = series[len(series) - 4 - offset: len(series) - offset]
    span = (window[-1].period_end - window[0].period_end).days
    if not 250 <= span <= 300:  # four consecutive quarters end ~273 days apart
        return None, None
    return sum(p.value for p in window), window[-1].period_end


def _safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b in (None, 0):
        return None
    return a / b


STALE_INSTANT_DAYS = 400
STALE_FLOW_DAYS = 200  # a TTM component ending this long before the latest income statement is ignored
SHARE_SCALES = (1.0, 1e3, 1e6)


def _fix_share_scale(snap: FinancialSnapshot) -> None:
    """Some filers tag share counts in thousands/millions while declaring the unit 'shares'
    (e.g. McDonald's reports 711.1 meaning 711.1 million). Detect the scale by comparing with
    the cover-page share count, or else net income / reported EPS, and rescale the series."""
    series = [s for s in (snap.quarterly.get("diluted_shares"), snap.annual.get("diluted_shares")) if s]
    if not series:
        return
    latest = series[0][-1].value
    reference: float | None = None
    basis = ""
    so = snap.instant.get("shares_outstanding")
    if so and so.value > 0:
        reference, basis = so.value, "cover-page shares outstanding"
    else:
        ni = {p.period_end: p.value for p in snap.annual.get("net_income", [])}
        for p in reversed(snap.annual.get("eps_diluted", [])):
            if p.period_end in ni and p.value and abs(p.value) > 0.01:
                reference, basis = ni[p.period_end] / p.value, "net income / diluted EPS"
                break
    if not reference or latest <= 0 or reference <= 0:
        return
    best = min(SHARE_SCALES, key=lambda f: abs(math.log(latest * f / reference)))
    if best == 1.0 or not 0.5 <= latest * best / reference <= 2.0:
        return
    for s in series:
        for i, p in enumerate(s):
            s[i] = PeriodValue(p.period_start, p.period_end, p.value * best, p.fiscal_year, p.fiscal_period,
                               p.accession, p.filed_date, p.derived, p.concept)
    snap.warnings.append(
        f"Diluted share counts were tagged in units of {int(best):,} by the filer; rescaled using {basis}.")


SHARE_METRICS = {"diluted_shares", "shares_outstanding"}
PER_SHARE_METRICS = {"eps_diluted", "eps_basic"}
TTM_METRICS = ("revenue", "net_income", "net_income_common", "preferred_dividends", "operating_income", "gross_profit",
               "operating_cash_flow", "capex", "capex_productive_assets", "capex_flight_equipment", "capex_oil_gas",
               "share_based_comp", "depreciation", "gain_on_property_sales", "dividends_paid")
CAPEX_COMPONENTS = ("capex", "capex_flight_equipment", "capex_oil_gas")
NI_CONSOLIDATED = "ProfitLoss"


def split_factor(splits: list[tuple[date, float]] | None, period_end: date, filed: date) -> float:
    """Cumulative split ratio (new shares per old share) for a fact not yet restated for later splits.

    Filers restate share and per-share data retroactively in filings made *after* a split, so a fact
    filed on/after the ex-date is already on the post-split basis; only earlier filings are adjusted.
    Every known split is applied (also for point-in-time snapshots): stored prices are split-adjusted to
    today's share basis, and price/EPS ratios must use the same basis. This uses no future
    information — a split changes neither the company's value nor any ratio."""
    f = 1.0
    for ex, ratio in splits or []:
        if period_end < ex and filed < ex and ratio > 0:
            f *= ratio
    return f


def _adjust_for_splits(facts: list[FinancialFact], splits: list[tuple[date, float]] | None,
                       notes: list[str]) -> list[FinancialFact]:
    if not splits:
        return facts
    out = []
    adjusted: dict[str, int] = {}
    for f in facts:
        if f.metric in SHARE_METRICS or f.metric in PER_SHARE_METRICS:
            k = split_factor(splits, f.period_end, f.filed_date)
            if k != 1.0:
                f = replace(f, value=f.value * k if f.metric in SHARE_METRICS else f.value / k)
                adjusted[f.metric] = adjusted.get(f.metric, 0) + 1
        out.append(f)
    for m, n in adjusted.items():
        notes.append(f"{n} '{m}' fact(s) filed before a later stock split were split-adjusted.")
    return out


def _combine(a: list[PeriodValue], b: list[PeriodValue], concept: str) -> list[PeriodValue]:
    """Per-period sum of two component series (only periods present in both)."""
    bm = {(p.period_start, p.period_end): p for p in b}
    out = []
    for p in a:
        q = bm.get((p.period_start, p.period_end))
        if q is not None:
            out.append(PeriodValue(p.period_start, p.period_end, p.value + q.value, p.fiscal_year, p.fiscal_period,
                                   max(p.accession, q.accession), max(p.filed_date, q.filed_date), True, concept))
    return out


def capex_total(productive: float | None, components: list[float | None]) -> float | None:
    """Capital expenditure = the larger of total productive-asset payments and the sum of the separately
    tagged PP&E / flight-equipment / oil & gas property payments (one may be a subtotal of the other, so
    the larger is used rather than adding them)."""
    comp = [abs(c) for c in components if c is not None]
    parts = [x for x in (abs(productive) if productive is not None else None, sum(comp) if comp else None) if x is not None]
    return max(parts) if parts else None


def _provenance(p: PeriodValue | None, basis: str) -> dict[str, Any] | None:
    if p is None:
        return None
    return {"concept": p.concept, "period_end": p.period_end.isoformat(), "fiscal_year": p.fiscal_year,
            "fiscal_period": p.fiscal_period, "filed": p.filed_date.isoformat(), "accession": p.accession,
            "derived": p.derived, "basis": basis}


def build_snapshot(facts: list[FinancialFact], as_of: date | None = None, currency: str = "USD",
                   splits: list[tuple[date, float]] | None = None) -> FinancialSnapshot:
    """Build the snapshot using only facts filed on/before `as_of` (point-in-time).

    `splits` are (ex_date, new-per-old ratio) pairs; share counts and per-share values filed before a
    later split are put on the current share basis so they are comparable with split-adjusted prices."""
    as_of = as_of or date.today()
    known = [f for f in facts if f.filed_date <= as_of]
    snap = FinancialSnapshot(as_of=as_of, currency=currency, financial_data_version=None, latest_filed=None)
    if not known:
        snap.warnings.append("No SEC XBRL financial facts available as of this date.")
        snap.financial_status = "data_unavailable"
        return snap
    known = _adjust_for_splits(known, splits, snap.split_adjustments)

    units = {f.unit for f in known if f.metric == "revenue"}
    if units:
        snap.currency = sorted(units)[0]
    if snap.currency != "USD":
        snap.warnings.append(
            f"Financial statements are reported in {snap.currency}, not USD. Per-share valuation against a "
            "USD share price requires FX and ADR-ratio conversion, which is not applied."
        )

    by_metric: dict[tuple[str, str], list[FinancialFact]] = {}
    for f in known:
        by_metric.setdefault((f.metric, f.period_type), []).append(f)
    for (metric, ptype), fs in by_metric.items():
        series = _dedupe_latest(fs)
        if ptype == "quarterly":
            snap.quarterly[metric] = series
        elif ptype == "annual":
            snap.annual[metric] = series
        elif series:
            snap.instant[metric] = series[-1]
            snap.instant_history[metric] = series

    # Balance-sheet items must come from (roughly) the latest balance sheet: a concept a company
    # stopped tagging years ago (e.g. short-term investments last reported in 2014) is dropped.
    if snap.instant:
        latest_bs = max(v.period_end for v in snap.instant.values())
        for metric in [m for m, v in snap.instant.items() if (latest_bs - v.period_end).days > STALE_INSTANT_DAYS]:
            snap.warnings.append(
                f"Ignored stale balance-sheet item '{metric}' (last reported {snap.instant[metric].period_end}).")
            del snap.instant[metric]

    _fix_share_scale(snap)

    # Industry revenue: banks and insurers often report no total-revenue concept. Components are
    # combined only when no total is reported for that period type (never mixed with a total).
    for ptype in ("quarterly", "annual"):
        store = snap.quarterly if ptype == "quarterly" else snap.annual
        if store.get("revenue"):
            continue
        for a, b, label in (("net_interest_income", "noninterest_income",
                             "bank revenue = net interest income + noninterest income"),
                            ("premiums_earned", "net_investment_income",
                             "insurance revenue = net premiums earned + net investment income")):
            combo = _combine(store.get(a, []), store.get(b, []), label)
            if combo:
                store["revenue"] = combo
                snap.warnings.append(f"No total-revenue concept reported ({ptype}); {label}.")
                break

    annual_forms = [f for f in known if f.period_type == "annual" and f.metric in ("revenue", "net_income") and f.form]
    if annual_forms and max(annual_forms, key=lambda f: f.filed_date).form.startswith(("20-F", "40-F")):
        snap.foreign_issuer = True
        snap.warnings.append(
            "Foreign private issuer (files 20-F/40-F): per-share figures are per ordinary share and the US "
            "listing may be an ADR representing several shares. Per-share valuation is disabled because the "
            "ADR ratio is not available from SEC XBRL.")

    latest = max(known, key=lambda f: (f.filed_date, f.accession))
    snap.latest_filed = latest.filed_date
    accns = sorted({f.accession for f in known})
    snap.accessions = accns[-12:]
    snap.financial_data_version = f"{latest.accession}@{latest.filed_date}#" + hashlib.sha1(
        ",".join(accns).encode()).hexdigest()[:8]

    # TTM: prefer 4 quarters; fall back to the latest annual when quarters are incomplete or older.
    for m in TTM_METRICS:
        q = snap.quarterly.get(m, [])
        ttm, end = _sum_last4(q)
        prior, _ = _sum_last4(q, offset=4)
        basis = "ttm_4_quarters"
        src = q[-1] if q and ttm is not None else None
        annual = snap.annual.get(m, [])
        if (ttm is None and annual) or (annual and end and annual[-1].period_end > end):
            ttm, end = annual[-1].value, annual[-1].period_end
            prior = annual[-2].value if len(annual) > 1 else None
            basis, src = "fiscal_year", annual[-1]
            if m == "revenue" and q:
                snap.warnings.append("Quarterly revenue incomplete; trailing figures use the latest fiscal year.")
        snap.ttm[m] = ttm
        snap.ttm_prior[m] = prior
        if src is not None:
            snap.provenance[m] = _provenance(src, basis)
        if m == "revenue":
            snap.ttm_period_end = end
    # A component whose latest period is far older than the latest income statement (e.g. a concept the
    # filer stopped tagging years ago) must not be mixed into current figures.
    anchor_end = max((date.fromisoformat(snap.provenance[m]["period_end"]) for m in ("revenue", "net_income")
                      if m in snap.provenance), default=None)
    if anchor_end is not None:
        for m in TTM_METRICS:
            pv = snap.provenance.get(m)
            if pv and (anchor_end - date.fromisoformat(pv["period_end"])).days > STALE_FLOW_DAYS:
                snap.warnings.append(f"Ignored stale '{m}' (last reported for the period ending {pv['period_end']}).")
                snap.ttm[m] = snap.ttm_prior[m] = None
                snap.provenance[m] = {**pv, "ignored": "stale"}
    if (snap.provenance.get("net_income") or {}).get("concept") == NI_CONSOLIDATED:
        snap.warnings.append("Net income uses the consolidated ProfitLoss concept (includes noncontrolling interests).")

    t = snap.ttm
    capex = capex_total(t.get("capex_productive_assets"), [t.get(c) for c in CAPEX_COMPONENTS])
    t["capex_total"] = capex
    ocf = t.get("operating_cash_flow")
    t["free_cash_flow"] = ocf - capex if ocf is not None and capex is not None else None
    sbc = t.get("share_based_comp")
    t["free_cash_flow_after_sbc"] = (t["free_cash_flow"] - abs(sbc)
                                     if t["free_cash_flow"] is not None and sbc is not None else None)
    if ocf is not None and capex is None:
        snap.warnings.append("Capital expenditures not found; free cash flow unavailable.")
    if t.get("capex_flight_equipment") or t.get("capex_oil_gas"):
        snap.warnings.append("Capital expenditure includes separately tagged flight-equipment / oil & gas property payments.")

    # Shares: latest diluted weighted average (quarterly or annual, whichever is more recent) for EPS;
    # the cover-page count is kept separately as *current shares outstanding* (used for market cap).
    q_dil, a_dil = snap.quarterly.get("diluted_shares") or [], snap.annual.get("diluted_shares") or []
    dil = max((q_dil, a_dil), key=lambda s: s[-1].period_end if s else date.min)
    shares = dil[-1].value if dil else None
    so = snap.instant.get("shares_outstanding")
    if so is not None and so.value <= 0:  # a zero/negative cover-page count is a tagging error, not data
        snap.validation.append(f"Ignored non-positive shares outstanding ({so.value:,.0f}).")
        so = None
    if shares is None and so is not None:
        shares = so.value
    t["diluted_shares"] = shares
    t["shares_outstanding"] = so.value if so else None
    if so:
        snap.provenance["shares_outstanding"] = _provenance(so, "instant")
        if dil and not 0.5 <= dil[-1].value / so.value <= 2.0:
            snap.validation.append(
                f"Diluted weighted shares ({dil[-1].value:,.0f}) and shares outstanding ({so.value:,.0f}) "
                "differ by more than 2x.")

    # EPS TTM: sum of 4 reported diluted quarterly EPS (Q4 isn't reported separately, so this
    # usually falls back to TTM net income to common / diluted shares).
    ni = t.get("net_income")
    ni_common = t.get("net_income_common")
    if ni_common is None and ni is not None and t.get("preferred_dividends"):
        ni_common = ni - abs(t["preferred_dividends"])
    t["net_income_to_common"] = ni_common if ni_common is not None else ni
    eps_q = snap.quarterly.get("eps_diluted", [])
    eps_ttm, eps_end = _sum_last4(eps_q)
    if eps_ttm is None or (snap.ttm_period_end and eps_end and eps_end < snap.ttm_period_end):
        eps_ttm = _safe_div(t["net_income_to_common"], shares)
    t["eps"] = eps_ttm
    prior_ni = snap.ttm_prior.get("net_income")
    prior_sh = dil[-5].value if len(dil) >= 5 else shares
    snap.ttm_prior["eps"] = _safe_div(prior_ni, prior_sh)

    cash = (snap.instant["cash"].value if "cash" in snap.instant else 0.0) + (
        snap.instant["short_term_investments"].value if "short_term_investments" in snap.instant else 0.0)
    debt = (snap.instant["long_term_debt"].value if "long_term_debt" in snap.instant else 0.0) + (
        snap.instant["current_debt"].value if "current_debt" in snap.instant else 0.0)
    t["cash_and_investments"] = cash if "cash" in snap.instant else None
    t["total_debt"] = debt if ("long_term_debt" in snap.instant or "current_debt" in snap.instant) else None
    if t["total_debt"] is None:
        snap.warnings.append("No debt balances tagged; debt treated as unknown (assumed 0 in valuation).")

    # Book value to common and tangible book value (goodwill/intangibles only when reported).
    eq = snap.instant.get("stockholders_equity")
    if eq is not None:
        pref = snap.instant["preferred_equity"].value if "preferred_equity" in snap.instant else 0.0
        gw = snap.instant.get("goodwill")
        ia = snap.instant.get("intangibles")
        t["book_value"] = eq.value - pref
        t["tangible_book_value"] = t["book_value"] - (gw.value if gw else 0.0) - (ia.value if ia else 0.0)
        snap.provenance["book_value"] = _provenance(eq, "instant")
        snap.provenance["tangible_book_value"] = {
            "basis": "stockholders' equity - preferred - goodwill - intangibles",
            "goodwill_reported": gw is not None, "intangibles_reported": ia is not None}
    else:
        t["book_value"] = t["tangible_book_value"] = None

    rev = t.get("revenue")
    r = snap.ratios
    r["revenue_growth_yoy"] = _safe_div(rev, snap.ttm_prior.get("revenue"))
    r["revenue_growth_yoy"] = r["revenue_growth_yoy"] - 1 if r["revenue_growth_yoy"] is not None else None
    eps_prior = snap.ttm_prior.get("eps")
    r["eps_growth_yoy"] = (eps_ttm / eps_prior - 1) if eps_ttm is not None and eps_prior and eps_prior > 0 else None
    r["gross_margin"] = _safe_div(t.get("gross_profit"), rev)
    r["operating_margin"] = _safe_div(t.get("operating_income"), rev)
    r["net_margin"] = _safe_div(ni, rev)
    r["fcf_margin"] = _safe_div(t.get("free_cash_flow"), rev)
    r["fcf_after_sbc_margin"] = _safe_div(t.get("free_cash_flow_after_sbc"), rev)
    r["capex_ratio"] = _safe_div(capex, rev)
    r["sbc_ratio"] = _safe_div(abs(sbc) if sbc is not None else None, rev)
    ntc = t["net_income_to_common"]
    r["roe"] = _safe_div(ntc, t["book_value"]) if (t.get("book_value") or 0) > 0 else None
    r["rotce"] = _safe_div(ntc, t["tangible_book_value"]) if (t.get("tangible_book_value") or 0) > 0 else None
    div = t.get("dividends_paid")
    r["payout_ratio"] = abs(div) / ntc if div is not None and (ntc or 0) > 0 else None

    annual_rev = snap.annual.get("revenue", [])
    if len(annual_rev) >= 4 and annual_rev[-4].value > 0 and annual_rev[-1].value > 0:
        r["revenue_cagr_3y"] = (annual_rev[-1].value / annual_rev[-4].value) ** (1 / 3) - 1
    elif len(annual_rev) >= 2 and annual_rev[-2].value > 0:
        r["revenue_cagr_3y"] = annual_rev[-1].value / annual_rev[-2].value - 1
    else:
        r["revenue_cagr_3y"] = None

    # Historical annual ratios (scenario ranges, normalization, cyclicality)
    def amap(m: str) -> dict[date, float]:
        return {p.period_end: p.value for p in snap.annual.get(m, [])}

    ann_ni, ann_oi, ann_ocf = amap("net_income"), amap("operating_income"), amap("operating_cash_flow")
    ann_sbc, ann_prod = amap("share_based_comp"), amap("capex_productive_assets")
    ann_comp = [amap(c) for c in CAPEX_COMPONENTS]
    eq_hist = {p.period_end: p.value for p in snap.instant_history.get("stockholders_equity", [])}
    hist = []
    for p in annual_rev[-8:]:
        if p.value <= 0:
            continue
        e = p.period_end
        has_cx = e in ann_prod or any(e in c for c in ann_comp)
        cx = capex_total(ann_prod.get(e), [c.get(e) for c in ann_comp]) if has_cx else None
        fcf = (ann_ocf[e] - cx) if e in ann_ocf and cx is not None else None
        hist.append({
            "period_end": e.isoformat(), "fiscal_year": p.fiscal_year, "revenue": p.value,
            "net_margin": ann_ni[e] / p.value if e in ann_ni else None,
            "operating_margin": ann_oi[e] / p.value if e in ann_oi else None,
            "fcf_margin": fcf / p.value if fcf is not None else None,
            "fcf_after_sbc_margin": (fcf - abs(ann_sbc[e])) / p.value if fcf is not None and e in ann_sbc else None,
            "roe": ann_ni[e] / eq_hist[e] if e in ann_ni and eq_hist.get(e, 0) > 0 else None,
        })
    snap.margin_history = hist[-6:]

    # annual share-count change rate (dilution or buybacks)
    ann_sh = snap.annual.get("diluted_shares", [])
    if len(ann_sh) >= 3 and ann_sh[-3].value > 0:
        r["share_change_rate"] = (ann_sh[-1].value / ann_sh[-3].value) ** 0.5 - 1
    elif len(ann_sh) >= 2 and ann_sh[-2].value > 0:
        r["share_change_rate"] = ann_sh[-1].value / ann_sh[-2].value - 1
    else:
        r["share_change_rate"] = None

    _validate(snap)
    return snap


def _validate(snap: FinancialSnapshot) -> None:
    """Flag inconsistent data and classify the financial-data status (missing is not the same as weak)."""
    t = snap.ttm
    rev, ni = t.get("revenue"), t.get("net_income")
    if rev is not None and rev <= 0:
        snap.validation.append(f"Non-positive trailing revenue ({rev:,.0f}).")
    # profit far above revenue indicates a tagging/scale error (large *losses* relative to small revenue are
    # normal for early-stage companies and are not flagged)
    if rev and rev > 0 and ni is not None and ni > 3 * rev:
        snap.validation.append("Net income exceeds three times revenue (inconsistent tagging or one-off gain).")
    # reported annual EPS vs net income / diluted shares for the latest common fiscal year
    eps_a = {p.period_end: p.value for p in snap.annual.get("eps_diluted", [])}
    sh_a = {p.period_end: p.value for p in snap.annual.get("diluted_shares", [])}
    ni_a = {p.period_end: p.value for p in snap.annual.get("net_income", [])}
    for e in sorted(set(eps_a) & set(sh_a) & set(ni_a))[-1:]:
        calc = ni_a[e] / sh_a[e] if sh_a[e] else None
        if calc and abs(eps_a[e]) > 0.05 and not 0.6 <= calc / eps_a[e] <= 1.6:
            snap.validation.append(
                f"Reported diluted EPS {eps_a[e]:.2f} for the fiscal year ending {e} is inconsistent with "
                f"net income / diluted shares ({calc:.2f}).")
    if snap.foreign_issuer:
        snap.financial_status = "unsupported_accounting"
    elif rev is None and ni is None:
        snap.financial_status = "data_unavailable"
    elif any(v.startswith(("Non-positive", "Net income exceeds")) for v in snap.validation):
        snap.financial_status = "invalid"
    else:
        snap.financial_status = "valid"


def margin_stats(history: list[dict[str, Any]], key: str = "net_margin") -> dict[str, Any]:
    """Median, dispersion and cyclicality of a historical annual ratio."""
    vals = [h[key] for h in history if h.get(key) is not None]
    if len(vals) < 3:
        return {"n": len(vals), "median": None, "cyclical": None}
    med = statistics.median(vals)
    sd = statistics.pstdev(vals)
    sign_changes = sum(1 for a, b in zip(vals, vals[1:], strict=False) if (a > 0) != (b > 0))
    cyc = sign_changes > 0 or (abs(med) > 1e-9 and sd / abs(med) > 0.5)
    return {"n": len(vals), "median": med, "min": min(vals), "max": max(vals), "stdev": sd, "cyclical": cyc}
