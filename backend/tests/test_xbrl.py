from datetime import date

import pytest

from app.providers.sec.xbrl import detect_reporting_currency, normalize_companyfacts
from app.services.financials import build_snapshot


def f(start, end, val, accn, form, filed, fy, fp):
    d = {"end": end, "val": val, "accn": accn, "form": form, "filed": filed, "fy": fy, "fp": fp}
    if start:
        d["start"] = start
    return d


A0, A1, A2, A3, A4, A5 = "A0-23-K", "A1-24-Q1", "A2-24-Q2", "A3-24-Q3", "A4-24-K", "A5-24-KA"


def companyfacts():
    rev_old = [f("2023-01-01", "2023-12-31", 400, A0, "10-K", "2024-02-15", 2023, "FY"),
               f("2022-01-01", "2022-12-31", 350, A0, "10-K", "2024-02-15", 2023, "FY")]
    rev_new = [
        f("2024-01-01", "2024-03-31", 100, A1, "10-Q", "2024-05-01", 2024, "Q1"),
        f("2023-01-01", "2023-03-31", 90, A1, "10-Q", "2024-05-01", 2024, "Q1"),
        f("2024-04-01", "2024-06-30", 110, A2, "10-Q", "2024-08-01", 2024, "Q2"),
        f("2024-01-01", "2024-06-30", 210, A2, "10-Q", "2024-08-01", 2024, "Q2"),
        f("2024-07-01", "2024-09-30", 120, A3, "10-Q", "2024-11-01", 2024, "Q3"),
        f("2024-01-01", "2024-09-30", 330, A3, "10-Q", "2024-11-01", 2024, "Q3"),
        f("2024-01-01", "2024-12-31", 460, A4, "10-K", "2025-02-15", 2024, "FY"),
        f("2023-01-01", "2023-12-31", 401, A4, "10-K", "2025-02-15", 2024, "FY"),  # comparative, new concept
        f("2024-01-01", "2024-12-31", 455, A5, "10-K/A", "2025-04-01", 2024, "FY"),  # restatement
    ]
    ocf = [  # cash flow is reported year-to-date only
        f("2024-01-01", "2024-03-31", 30, A1, "10-Q", "2024-05-01", 2024, "Q1"),
        f("2024-01-01", "2024-06-30", 70, A2, "10-Q", "2024-08-01", 2024, "Q2"),
        f("2024-01-01", "2024-09-30", 100, A3, "10-Q", "2024-11-01", 2024, "Q3"),
        f("2024-01-01", "2024-12-31", 150, A4, "10-K", "2025-02-15", 2024, "FY"),
    ]
    eps = [f("2024-01-01", "2024-03-31", 1.0, A1, "10-Q", "2024-05-01", 2024, "Q1"),
           f("2024-04-01", "2024-06-30", 1.1, A2, "10-Q", "2024-08-01", 2024, "Q2"),
           f("2024-01-01", "2024-06-30", 2.1, A2, "10-Q", "2024-08-01", 2024, "Q2"),
           f("2024-01-01", "2024-12-31", 4.6, A4, "10-K", "2025-02-15", 2024, "FY")]
    cash = [f(None, "2024-12-31", 80, A4, "10-K", "2025-02-15", 2024, "FY")]
    dei_shares = [f(None, "2025-02-01", 99, A4, "10-K", "2025-02-15", 2024, "FY")]  # cover-page date
    ni = [f("2024-01-01", "2024-12-31", 46, A4, "10-K", "2025-02-15", 2024, "FY")]
    return {"cik": 1, "entityName": "Test Co", "facts": {
        "us-gaap": {
            "Revenues": {"units": {"USD": rev_old}},
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": rev_new}},
            "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": ocf}},
            "EarningsPerShareDiluted": {"units": {"USD/shares": eps}},
            "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": cash}},
            "NetIncomeLoss": {"units": {"USD": ni}},
        },
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": dei_shares}}},
    }}


def by(facts, metric, ptype):
    return sorted([x for x in facts if x.metric == metric and x.period_type == ptype],
                  key=lambda x: (x.period_end, x.filed_date))


def test_ytd_cash_flow_is_derived_into_quarters_not_mixed():
    facts = normalize_companyfacts(companyfacts())
    q = {(x.period_end, x.derived): x.value for x in by(facts, "operating_cash_flow", "quarterly")}
    assert q[(date(2024, 3, 31), False)] == 30
    assert q[(date(2024, 6, 30), True)] == 40  # H1 70 - Q1 30
    assert q[(date(2024, 9, 30), True)] == 30  # 9M 100 - H1 70
    assert q[(date(2024, 12, 31), True)] == 50  # FY 150 - 9M 100
    # the YTD values themselves never appear as quarters
    assert 70 not in [x.value for x in by(facts, "operating_cash_flow", "quarterly")]


def test_q4_revenue_derived_and_direct_quarters_not_duplicated():
    facts = normalize_companyfacts(companyfacts())
    q = by(facts, "revenue", "quarterly")
    q2 = [x for x in q if x.period_end == date(2024, 6, 30)]
    assert len(q2) == 1 and not q2[0].derived  # reported directly, so no derived duplicate
    q4 = [x for x in q if x.period_end == date(2024, 12, 31)]
    assert {x.value for x in q4} == {130, 125}  # original 10-K and restated 10-K/A
    assert all(x.derived and x.fiscal_period == "Q4" and x.fiscal_year == 2024 for x in q4)


def test_per_share_values_are_never_derived():
    facts = normalize_companyfacts(companyfacts())
    eps_q = by(facts, "eps_diluted", "quarterly")
    assert all(not x.derived for x in eps_q)
    assert date(2024, 12, 31) not in [x.period_end for x in eps_q]


def test_fiscal_labels_come_from_each_filings_own_period():
    facts = normalize_companyfacts(companyfacts())
    q1_2023 = [x for x in by(facts, "revenue", "quarterly") if x.period_end == date(2023, 3, 31)][0]
    q1_2024 = [x for x in by(facts, "revenue", "quarterly") if x.period_end == date(2024, 3, 31)][0]
    assert (q1_2024.fiscal_year, q1_2024.fiscal_period) == (2024, "Q1")
    # a comparative inside a FY2024 filing must NOT be labeled FY2024
    assert q1_2023.fiscal_year == 2023 and q1_2023.fiscal_period is None
    fy = by(facts, "revenue", "annual")
    assert (2024, 2024) in {(x.period_end.year, x.fiscal_year) for x in fy}


def test_concept_priority_and_provenance():
    facts = normalize_companyfacts(companyfacts())
    fy23 = [x for x in by(facts, "revenue", "annual") if x.period_end == date(2023, 12, 31)]
    assert {x.concept for x in fy23} == {"RevenueFromContractWithCustomerExcludingAssessedTax"}
    assert all(x.accession and x.filed_date and x.unit and x.form for x in facts)


def test_point_in_time_snapshot_avoids_look_ahead():
    facts = normalize_companyfacts(companyfacts())
    before = build_snapshot(facts, as_of=date(2025, 3, 1))
    after = build_snapshot(facts, as_of=date(2025, 5, 1))
    assert before.annual["revenue"][-1].value == 460
    assert after.annual["revenue"][-1].value == 455  # restatement visible only after it was filed
    mid = build_snapshot(facts, as_of=date(2024, 8, 15))
    assert mid.quarterly["revenue"][-1].period_end == date(2024, 6, 30)
    assert "operating_cash_flow" not in mid.annual


def test_snapshot_ttm_and_ratios():
    facts = normalize_companyfacts(companyfacts())
    s = build_snapshot(facts, as_of=date(2025, 3, 1))
    assert s.ttm["revenue"] == 460  # 100 + 110 + 120 + 130
    assert s.ttm["operating_cash_flow"] == 150
    assert s.ttm["free_cash_flow"] is None and any("Capital expenditures" in w for w in s.warnings)
    assert s.ttm["eps"] == pytest.approx(46 / 99)  # NI / shares (Q4 EPS is not reported)
    assert s.ratios["net_margin"] == pytest.approx(0.1)
    assert s.instant["cash"].value == 80


def test_non_usd_reporting_currency_detected():
    cf = {"facts": {"ifrs-full": {"Revenue": {"units": {"EUR": [
        f("2024-01-01", "2024-12-31", 10, "X", "20-F", "2025-03-01", 2024, "FY")]}}}}}
    assert detect_reporting_currency(cf) == "EUR"
    s = build_snapshot(normalize_companyfacts(cf), as_of=date(2025, 6, 1))
    assert s.currency == "EUR" and s.foreign_issuer
    assert any("not USD" in w for w in s.warnings)


def test_empty_and_unusual_statements():
    s = build_snapshot(normalize_companyfacts({"facts": {}}))
    assert not s.has_data and s.warnings
    weird = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        f("2024-01-01", "2024-02-15", 5, "W", "10-Q", "2024-04-01", 2024, "Q1"),  # 45-day period: unclassifiable
        f("2024-01-01", "2024-12-31", 10, "W2", "8-K", "2025-01-10", 2024, "FY"),  # non-periodic form ignored
    ]}}}}}
    assert normalize_companyfacts(weird) == []


def test_recorded_nike_companyfacts(fixture):
    """Recorded SEC data. Nike's fiscal year ends May 31."""
    facts = normalize_companyfacts(fixture("sec_companyfacts_nke.json"))
    s = build_snapshot(facts, as_of=date(2026, 10, 12))
    fy25 = [p for p in s.annual["revenue"] if p.period_end == date(2025, 5, 31)][0]
    assert fy25.fiscal_year == 2025 and fy25.value == pytest.approx(46.309e9, rel=0.01)
    q = s.quarterly["operating_cash_flow"]
    assert any(p.derived for p in q) and any(not p.derived for p in q)
    assert s.ttm["revenue"] > 40e9 and s.currency == "USD" and not s.foreign_issuer


def test_recorded_nvidia_fiscal_quarters(fixture):
    facts = normalize_companyfacts(fixture("sec_companyfacts_nvda.json"))
    s = build_snapshot(facts, as_of=date(2026, 10, 12))
    labels = {(p.period_end, p.fiscal_year, p.fiscal_period) for p in s.quarterly["revenue"]}
    assert (date(2026, 4, 26), 2027, "Q1") in labels
    assert (date(2026, 1, 25), 2026, "Q4") in labels
