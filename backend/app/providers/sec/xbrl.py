"""Normalize SEC XBRL CompanyFacts into a consistent long-format financial model.

Key behaviors
  * Companies use different concepts for the same metric (and switch over time, e.g.
    `Revenues` -> `RevenueFromContractWithCustomerExcludingAssessedTax` after ASC 606),
    so each metric has a priority-ordered concept list, merged per period.
  * Duration facts are classified by length: quarter, half-year YTD, nine-month YTD, annual.
  * Cash-flow statements are reported year-to-date in 10-Qs. Quarterly values are
    *derived* by subtracting the prior cumulative period with the same start date
    (never by treating YTD values as quarters). Q4 is derived as FY - 9M.
  * Per-share and share-count metrics are non-additive and are never derived.
  * Every fact keeps accession number, form, filed date, unit and concept, so the data
    can be queried "as known on date X" (point-in-time) to avoid look-ahead bias.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

# metric -> (period kind, unit family, [(taxonomy, concept), ...] in priority order)
FLOW = "duration"
INSTANT = "instant"

METRICS: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
    "revenue": (FLOW, "currency", [
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "Revenues"),
        ("us-gaap", "SalesRevenueNet"),
        ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
        ("us-gaap", "SalesRevenueGoodsNet"),
        ("us-gaap", "RevenuesNetOfInterestExpense"),
        ("us-gaap", "RegulatedAndUnregulatedOperatingRevenue"),
        ("us-gaap", "RealEstateRevenueNet"),
        ("ifrs-full", "Revenue"),
    ]),
    # Industry revenue components. Only combined into revenue when no total-revenue concept is
    # reported for the period (banks: NII + noninterest income; insurers: premiums + investment income).
    "net_interest_income": (FLOW, "currency", [("us-gaap", "InterestIncomeExpenseNet")]),
    "noninterest_income": (FLOW, "currency", [("us-gaap", "NoninterestIncome")]),
    "premiums_earned": (FLOW, "currency", [("us-gaap", "PremiumsEarnedNet")]),
    "net_investment_income": (FLOW, "currency", [("us-gaap", "NetInvestmentIncome")]),
    "gross_profit": (FLOW, "currency", [("us-gaap", "GrossProfit"), ("ifrs-full", "GrossProfit")]),
    "operating_income": (FLOW, "currency", [
        ("us-gaap", "OperatingIncomeLoss"), ("ifrs-full", "ProfitLossFromOperatingActivities"),
    ]),
    "net_income": (FLOW, "currency", [
        ("us-gaap", "NetIncomeLoss"),
        ("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic"),
        ("us-gaap", "ProfitLoss"),  # consolidated, incl. noncontrolling interests (flagged when used)
        ("ifrs-full", "ProfitLossAttributableToOwnersOfParent"),
        ("ifrs-full", "ProfitLoss"),
    ]),
    "net_income_common": (FLOW, "currency", [
        ("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic"),
        ("ifrs-full", "ProfitLossAttributableToOrdinaryEquityHoldersOfParentEntity"),
    ]),
    "preferred_dividends": (FLOW, "currency", [("us-gaap", "PreferredStockDividendsIncomeStatementImpact")]),
    "eps_diluted": (FLOW, "per_share", [
        ("us-gaap", "EarningsPerShareDiluted"),
        ("us-gaap", "EarningsPerShareBasicAndDiluted"),
        ("ifrs-full", "DilutedEarningsLossPerShare"),
    ]),
    "eps_basic": (FLOW, "per_share", [
        ("us-gaap", "EarningsPerShareBasic"), ("ifrs-full", "BasicEarningsLossPerShare"),
    ]),
    "operating_cash_flow": (FLOW, "currency", [
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
        ("ifrs-full", "CashFlowsFromUsedInOperatingActivities"),
    ]),
    # Capital expenditure is often split across separate cash-flow lines (e.g. airlines tag aircraft
    # purchases as PaymentsForFlightEquipment). Components are kept as separate metrics and combined
    # in the snapshot (see financials.capex_total).
    "capex": (FLOW, "currency", [
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
        ("us-gaap", "PaymentsForCapitalImprovements"),
        ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
    ]),
    "capex_productive_assets": (FLOW, "currency", [("us-gaap", "PaymentsToAcquireProductiveAssets")]),
    "capex_flight_equipment": (FLOW, "currency", [("us-gaap", "PaymentsForFlightEquipment")]),
    "capex_oil_gas": (FLOW, "currency", [
        ("us-gaap", "PaymentsToAcquireOilAndGasPropertyAndEquipment"),
        ("us-gaap", "PaymentsToExploreAndDevelopOilAndGasProperties"),
    ]),
    "share_based_comp": (FLOW, "currency", [
        ("us-gaap", "ShareBasedCompensation"), ("us-gaap", "AllocatedShareBasedCompensationExpense"),
        ("ifrs-full", "AdjustmentsForSharebasedPayments"),
    ]),
    "depreciation": (FLOW, "currency", [
        ("us-gaap", "DepreciationDepletionAndAmortization"), ("us-gaap", "DepreciationAndAmortization"),
        ("us-gaap", "DepreciationAmortizationAndAccretionNet"), ("us-gaap", "Depreciation"),
    ]),
    "gain_on_property_sales": (FLOW, "currency", [
        ("us-gaap", "GainsLossesOnSalesOfInvestmentRealEstate"),
        ("us-gaap", "GainLossOnSaleOfPropertyPlantEquipment"),
    ]),
    "dividends_paid": (FLOW, "currency", [
        ("us-gaap", "PaymentsOfDividendsCommonStock"), ("us-gaap", "PaymentsOfDividends"),
    ]),
    "diluted_shares": (FLOW, "shares", [
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
        ("ifrs-full", "AdjustedWeightedAverageShares"),
    ]),
    "total_assets": (INSTANT, "currency", [("us-gaap", "Assets"), ("ifrs-full", "Assets")]),
    "total_liabilities": (INSTANT, "currency", [("us-gaap", "Liabilities"), ("ifrs-full", "Liabilities")]),
    "cash": (INSTANT, "currency", [
        ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
        ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
        ("us-gaap", "Cash"),
        ("ifrs-full", "CashAndCashEquivalents"),
    ]),
    "short_term_investments": (INSTANT, "currency", [
        ("us-gaap", "ShortTermInvestments"),
        ("us-gaap", "MarketableSecuritiesCurrent"),
        ("us-gaap", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"),
    ]),
    "long_term_debt": (INSTANT, "currency", [
        ("us-gaap", "LongTermDebtNoncurrent"),
        ("us-gaap", "LongTermDebt"),
        ("us-gaap", "LongTermDebtAndCapitalLeaseObligations"),
        ("ifrs-full", "NoncurrentPortionOfNoncurrentBorrowings"),
    ]),
    "current_debt": (INSTANT, "currency", [
        ("us-gaap", "DebtCurrent"),
        ("us-gaap", "LongTermDebtCurrent"),
        ("us-gaap", "ShortTermBorrowings"),
        ("us-gaap", "CommercialPaper"),
        ("ifrs-full", "CurrentPortionOfNoncurrentBorrowings"),
    ]),
    "stockholders_equity": (INSTANT, "currency", [
        ("us-gaap", "StockholdersEquity"), ("ifrs-full", "EquityAttributableToOwnersOfParent"),
        ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    ]),
    "goodwill": (INSTANT, "currency", [("us-gaap", "Goodwill"), ("ifrs-full", "Goodwill")]),
    "intangibles": (INSTANT, "currency", [
        ("us-gaap", "IntangibleAssetsNetExcludingGoodwill"), ("us-gaap", "FiniteLivedIntangibleAssetsNet"),
    ]),
    "preferred_equity": (INSTANT, "currency", [
        ("us-gaap", "PreferredStockValue"), ("us-gaap", "PreferredStockValueOutstanding"),
    ]),
    "shares_outstanding": (INSTANT, "shares", [
        ("dei", "EntityCommonStockSharesOutstanding"), ("us-gaap", "CommonStockSharesOutstanding"),
    ]),
}

NON_ADDITIVE = {"eps_diluted", "eps_basic", "diluted_shares"}
ACCEPTED_FORMS = ("10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "20-F/A", "40-F", "10-KT")

# duration classification (days between start and end)
KIND_RANGES = {"Q": (75, 105), "H": (165, 200), "9M": (255, 290), "FY": (340, 380)}
STEP_PRIOR = {"H": "Q", "9M": "H", "FY": "9M"}


@dataclass(frozen=True)
class FinancialFact:
    metric: str
    period_type: str  # annual | quarterly | instant
    period_start: date | None
    period_end: date
    value: float
    unit: str
    concept: str
    taxonomy: str
    accession: str
    form: str | None
    filed_date: date
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    derived: bool = False
    derivation: str | None = None


@dataclass(frozen=True)
class _Raw:
    start: date | None
    end: date
    val: float
    accn: str
    form: str | None
    filed: date
    fy: int | None
    fp: str | None
    concept: str
    taxonomy: str
    unit: str

    @property
    def kind(self) -> str | None:
        if self.start is None:
            return "I"
        days = (self.end - self.start).days
        for k, (lo, hi) in KIND_RANGES.items():
            if lo <= days <= hi:
                return k
        return None


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _pick_unit(units: dict[str, list], family: str, preferred_currency: str | None) -> str | None:
    keys = list(units)
    if family == "shares":
        return "shares" if "shares" in units else None
    if family == "per_share":
        per = [k for k in keys if "/shares" in k]
        if preferred_currency and f"{preferred_currency}/shares" in per:
            return f"{preferred_currency}/shares"
        return "USD/shares" if "USD/shares" in per else (per[0] if per else None)
    money = [k for k in keys if "/" not in k and k not in ("shares", "pure")]
    if preferred_currency and preferred_currency in money:
        return preferred_currency
    if "USD" in money:
        return "USD"
    return money[0] if money else None


def detect_reporting_currency(companyfacts: dict[str, Any]) -> str:
    facts = companyfacts.get("facts", {})
    for tax, concept in METRICS["revenue"][2] + METRICS["total_assets"][2]:
        units = facts.get(tax, {}).get(concept, {}).get("units", {})
        money = [k for k in units if "/" not in k and k not in ("shares", "pure")]
        if money:
            return "USD" if "USD" in money else money[0]
    return "USD"


def _fiscal_labels(raws: list[_Raw]) -> dict[tuple[date, str], tuple[int | None, str | None]]:
    """Map (period_end, kind_family) -> (fy, fp) using each filing's own current period.

    In CompanyFacts, `fy`/`fp` describe the *filing*, not the fact (a FY2024 10-K also
    carries FY2023 comparatives tagged fy=2024). So a label is only trusted for facts whose
    end date equals the latest end date in that accession."""
    # Only duration facts define the filing's period: cover-page instants (e.g. dei shares
    # outstanding "as of" a later date) would otherwise push the period end past the quarter.
    max_end: dict[str, date] = {}
    for r in raws:
        if r.start is not None and r.end > max_end.get(r.accn, date.min):
            max_end[r.accn] = r.end
    labels: dict[tuple[date, str], tuple[int | None, str | None]] = {}
    for r in raws:
        if r.accn not in max_end or r.end != max_end[r.accn] or r.fy is None:
            continue
        family = "annual" if r.fp == "FY" else "quarter"
        labels.setdefault((r.end, family), (r.fy, r.fp))
        labels.setdefault((r.end, "instant"), (r.fy, r.fp))
    return labels


def _collect(companyfacts: dict[str, Any], metric: str, currency: str) -> list[_Raw]:
    _, family, concepts = METRICS[metric]
    facts = companyfacts.get("facts", {})
    out: list[_Raw] = []
    for priority, (tax, concept) in enumerate(concepts):
        node = facts.get(tax, {}).get(concept)
        if not node:
            continue
        unit = _pick_unit(node.get("units", {}), family, currency)
        if not unit:
            continue
        for f in node["units"][unit]:
            form = f.get("form")
            if form and not form.startswith(ACCEPTED_FORMS):
                continue
            try:
                out.append(_Raw(
                    start=_d(f.get("start")), end=_d(f["end"]), val=float(f["val"]), accn=f["accn"],
                    form=form, filed=_d(f["filed"]), fy=f.get("fy"), fp=f.get("fp"),
                    concept=f"{priority}:{concept}", taxonomy=tax, unit=unit,
                ))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _merge_concepts(raws: list[_Raw]) -> list[_Raw]:
    """For each (start, end, accession) keep only the highest-priority concept."""
    best: dict[tuple, _Raw] = {}
    for r in raws:
        key = (r.start, r.end, r.accn)
        cur = best.get(key)
        if cur is None or int(r.concept.split(":")[0]) < int(cur.concept.split(":")[0]):
            best[key] = r
    # also: when two different concepts cover the same period in *different* filings, prefer the
    # higher-priority one if both exist for the period (avoid mixing definitions within a period)
    by_period: dict[tuple, int] = {}
    for r in best.values():
        p = int(r.concept.split(":")[0])
        k = (r.start, r.end)
        by_period[k] = min(by_period.get(k, 99), p)
    return [r for r in best.values() if int(r.concept.split(":")[0]) == by_period[(r.start, r.end)]]


def _concept_name(r: _Raw) -> str:
    return r.concept.split(":", 1)[1]


def normalize_companyfacts(companyfacts: dict[str, Any]) -> list[FinancialFact]:
    currency = detect_reporting_currency(companyfacts)
    all_raw: list[_Raw] = []
    per_metric: dict[str, list[_Raw]] = {}
    for metric in METRICS:
        raws = _merge_concepts(_collect(companyfacts, metric, currency))
        per_metric[metric] = raws
        all_raw.extend(raws)
    labels = _fiscal_labels(all_raw)

    out: list[FinancialFact] = []
    for metric, raws in per_metric.items():
        kind_kw = METRICS[metric][0]
        if kind_kw == INSTANT:
            for r in raws:
                if r.start is not None:
                    continue
                fy, fp = labels.get((r.end, "instant"), (None, None))
                out.append(_fact(metric, "instant", r, fy, fp))
            continue
        out.extend(_normalize_flow(metric, raws, labels))
    return out


def _fact(metric: str, ptype: str, r: _Raw, fy: int | None, fp: str | None, **kw: Any) -> FinancialFact:
    return FinancialFact(
        metric=metric, period_type=ptype, period_start=kw.get("start", r.start), period_end=r.end,
        value=kw.get("value", r.val), unit=r.unit, concept=_concept_name(r), taxonomy=r.taxonomy,
        accession=r.accn, form=r.form, filed_date=r.filed,
        fiscal_year=fy if fy is not None else r.end.year, fiscal_period=fp,
        derived=kw.get("derived", False), derivation=kw.get("derivation"),
    )


def _known_at(cands: list[_Raw], when: date) -> _Raw | None:
    """Latest-filed candidate known on `when` (point-in-time)."""
    known = [c for c in cands if c.filed <= when]
    return max(known, key=lambda c: c.filed) if known else None


def _normalize_flow(metric: str, raws: list[_Raw], labels: dict) -> list[FinancialFact]:
    out: list[FinancialFact] = []
    by_kind: dict[str, list[_Raw]] = defaultdict(list)
    for r in raws:
        k = r.kind
        if k and k != "I":
            by_kind[k].append(r)

    for r in by_kind["FY"]:
        fy, fp = labels.get((r.end, "annual"), (None, "FY"))
        out.append(_fact(metric, "annual", r, fy, "FY" if fp is None else fp))

    direct_q_ends: dict[date, list[_Raw]] = defaultdict(list)
    for r in by_kind["Q"]:
        direct_q_ends[r.end].append(r)
        fy, fp = labels.get((r.end, "quarter"), (None, None))
        out.append(_fact(metric, "quarterly", r, fy, fp))

    if metric in NON_ADDITIVE:
        return out

    # Derive quarters from cumulative YTD facts: Q = YTD(n) - YTD(n-1) with the same start date.
    cumul: dict[tuple[str, date | None], list[_Raw]] = defaultdict(list)
    for k in ("Q", "H", "9M"):
        for r in by_kind[k]:
            cumul[(k, r.start)].append(r)
    for k in ("H", "9M", "FY"):
        prior_kind = STEP_PRIOR[k]
        for r in by_kind[k]:
            prior_cands = cumul.get((prior_kind, r.start), [])
            prior = _known_at(prior_cands, r.filed)
            if prior is None:
                continue
            q_start = prior.end + timedelta(days=1)
            if not (KIND_RANGES["Q"][0] <= (r.end - q_start).days <= KIND_RANGES["Q"][1]):
                continue
            # Skip if a directly reported quarter for this period was known at this filing date.
            if any(d.filed <= r.filed and abs((d.start - q_start).days) <= 3 for d in direct_q_ends.get(r.end, [])):
                continue
            fy, fp = labels.get((r.end, "quarter"), (None, None))
            if k == "FY":
                fy_label = labels.get((r.end, "annual"), (None, None))[0]
                fy, fp = fy_label, "Q4"
            out.append(_fact(
                metric, "quarterly", r, fy, fp, start=q_start, value=r.val - prior.val, derived=True,
                derivation=f"{k} ({r.accn}) minus {prior_kind} ({prior.accn})",
            ))
    return out
