"""Persist normalized SEC facts and build point-in-time financial snapshots."""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
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


def build_snapshot(facts: list[FinancialFact], as_of: date | None = None, currency: str = "USD") -> FinancialSnapshot:
    """Build the snapshot using only facts filed on/before `as_of` (point-in-time)."""
    as_of = as_of or date.today()
    known = [f for f in facts if f.filed_date <= as_of]
    snap = FinancialSnapshot(as_of=as_of, currency=currency, financial_data_version=None, latest_filed=None)
    if not known:
        snap.warnings.append("No SEC XBRL financial facts available as of this date.")
        return snap

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

    # Balance-sheet items must come from (roughly) the latest balance sheet: a concept a company
    # stopped tagging years ago (e.g. short-term investments last reported in 2014) is dropped.
    if snap.instant:
        latest_bs = max(v.period_end for v in snap.instant.values())
        for metric in [m for m, v in snap.instant.items() if (latest_bs - v.period_end).days > STALE_INSTANT_DAYS]:
            snap.warnings.append(
                f"Ignored stale balance-sheet item '{metric}' (last reported {snap.instant[metric].period_end}).")
            del snap.instant[metric]

    _fix_share_scale(snap)

    annual_forms = [f for f in known if f.period_type == "annual" and f.metric == "revenue" and f.form]
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

    # TTM: prefer 4 quarters; fall back to the latest annual when quarters are incomplete.
    for m in ("revenue", "net_income", "operating_income", "gross_profit", "operating_cash_flow", "capex"):
        q = snap.quarterly.get(m, [])
        ttm, end = _sum_last4(q)
        prior, _ = _sum_last4(q, offset=4)
        annual = snap.annual.get(m, [])
        if ttm is None and annual:
            ttm, end = annual[-1].value, annual[-1].period_end
            prior = annual[-2].value if len(annual) > 1 else None
            if m == "revenue":
                snap.warnings.append("Quarterly revenue incomplete; trailing figures use the latest fiscal year.")
        elif annual and end and annual[-1].period_end > end:
            ttm, end = annual[-1].value, annual[-1].period_end
            prior = annual[-2].value if len(annual) > 1 else None
        snap.ttm[m] = ttm
        snap.ttm_prior[m] = prior
        if m == "revenue":
            snap.ttm_period_end = end

    ocf, capex = snap.ttm.get("operating_cash_flow"), snap.ttm.get("capex")
    snap.ttm["free_cash_flow"] = ocf - abs(capex) if ocf is not None and capex is not None else None
    if ocf is not None and capex is None:
        snap.warnings.append("Capital expenditures not found; free cash flow unavailable.")

    # Shares: latest diluted weighted average, else shares outstanding cover.
    dil = snap.quarterly.get("diluted_shares") or snap.annual.get("diluted_shares") or []
    shares = dil[-1].value if dil else None
    if shares is None and "shares_outstanding" in snap.instant:
        shares = snap.instant["shares_outstanding"].value
    snap.ttm["diluted_shares"] = shares

    # EPS TTM: sum of 4 reported diluted quarterly EPS (Q4 isn't reported separately, so this
    # usually falls back to TTM net income / diluted shares).
    eps_q = snap.quarterly.get("eps_diluted", [])
    eps_ttm, eps_end = _sum_last4(eps_q)
    if eps_ttm is None or (snap.ttm_period_end and eps_end and eps_end < snap.ttm_period_end):
        eps_ttm = _safe_div(snap.ttm.get("net_income"), shares)
    snap.ttm["eps"] = eps_ttm
    prior_ni = snap.ttm_prior.get("net_income")
    prior_sh = dil[-5].value if len(dil) >= 5 else shares
    snap.ttm_prior["eps"] = _safe_div(prior_ni, prior_sh)

    cash = (snap.instant["cash"].value if "cash" in snap.instant else 0.0) + (
        snap.instant["short_term_investments"].value if "short_term_investments" in snap.instant else 0.0)
    debt = (snap.instant["long_term_debt"].value if "long_term_debt" in snap.instant else 0.0) + (
        snap.instant["current_debt"].value if "current_debt" in snap.instant else 0.0)
    snap.ttm["cash_and_investments"] = cash if "cash" in snap.instant else None
    snap.ttm["total_debt"] = debt if ("long_term_debt" in snap.instant or "current_debt" in snap.instant) else None
    if snap.ttm["total_debt"] is None:
        snap.warnings.append("No debt balances tagged; debt treated as unknown (assumed 0 in valuation).")

    rev = snap.ttm.get("revenue")
    r = snap.ratios
    r["revenue_growth_yoy"] = _safe_div(rev, snap.ttm_prior.get("revenue"))
    r["revenue_growth_yoy"] = r["revenue_growth_yoy"] - 1 if r["revenue_growth_yoy"] is not None else None
    eps_prior = snap.ttm_prior.get("eps")
    r["eps_growth_yoy"] = (eps_ttm / eps_prior - 1) if eps_ttm is not None and eps_prior and eps_prior > 0 else None
    r["gross_margin"] = _safe_div(snap.ttm.get("gross_profit"), rev)
    r["operating_margin"] = _safe_div(snap.ttm.get("operating_income"), rev)
    r["net_margin"] = _safe_div(snap.ttm.get("net_income"), rev)
    r["fcf_margin"] = _safe_div(snap.ttm.get("free_cash_flow"), rev)

    annual_rev = snap.annual.get("revenue", [])
    if len(annual_rev) >= 4 and annual_rev[-4].value > 0 and annual_rev[-1].value > 0:
        r["revenue_cagr_3y"] = (annual_rev[-1].value / annual_rev[-4].value) ** (1 / 3) - 1
    elif len(annual_rev) >= 2 and annual_rev[-2].value > 0:
        r["revenue_cagr_3y"] = annual_rev[-1].value / annual_rev[-2].value - 1
    else:
        r["revenue_cagr_3y"] = None

    # Historical annual margins (for scenario ranges)
    ann_ni = {p.period_end: p.value for p in snap.annual.get("net_income", [])}
    ann_oi = {p.period_end: p.value for p in snap.annual.get("operating_income", [])}
    ann_ocf = {p.period_end: p.value for p in snap.annual.get("operating_cash_flow", [])}
    ann_cx = {p.period_end: p.value for p in snap.annual.get("capex", [])}
    hist = []
    for p in annual_rev[-6:]:
        if p.value <= 0:
            continue
        fcf = (ann_ocf[p.period_end] - abs(ann_cx[p.period_end])) if p.period_end in ann_ocf and p.period_end in ann_cx else None
        hist.append({
            "period_end": p.period_end.isoformat(), "fiscal_year": p.fiscal_year, "revenue": p.value,
            "net_margin": ann_ni[p.period_end] / p.value if p.period_end in ann_ni else None,
            "operating_margin": ann_oi[p.period_end] / p.value if p.period_end in ann_oi else None,
            "fcf_margin": fcf / p.value if fcf is not None else None,
        })
    snap.margin_history = hist

    # annual share-count change rate (dilution or buybacks)
    ann_sh = snap.annual.get("diluted_shares", [])
    if len(ann_sh) >= 3 and ann_sh[-3].value > 0:
        r["share_change_rate"] = (ann_sh[-1].value / ann_sh[-3].value) ** 0.5 - 1
    elif len(ann_sh) >= 2 and ann_sh[-2].value > 0:
        r["share_change_rate"] = ann_sh[-1].value / ann_sh[-2].value - 1
    else:
        r["share_change_rate"] = None
    return snap
