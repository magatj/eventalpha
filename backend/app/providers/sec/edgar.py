"""SEC EDGAR client (https://www.sec.gov/search-filings/edgar-application-programming-interfaces).

Respects SEC fair-access policy: descriptive User-Agent with contact info, <=10 req/s
(we default to 5), cached responses, and no scraping of HTML pages.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel

from app.config import Settings
from app.services.http import DataUnavailable, HttpClient, ProviderError, RateLimiter, ResponseCache

PROVIDER = "sec_edgar"
TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_nodash}/{doc}"

MATERIAL_FORMS = {"8-K", "10-K", "10-Q", "20-F", "6-K", "40-F", "S-1", "S-4", "SC 13D", "DEF 14A", "424B4"}


class TickerIdentity(BaseModel):
    ticker: str
    cik: str  # zero-padded 10 digits
    name: str
    exchange: str | None = None


class Filing(BaseModel):
    accession: str
    form: str
    filed_date: date
    report_date: date | None = None
    primary_document: str | None = None
    items: str | None = None
    url: str


class CompanySubmissions(BaseModel):
    cik: str
    name: str
    sic: str | None = None
    sic_description: str | None = None
    fiscal_year_end: str | None = None
    exchanges: list[str] = []
    tickers: list[str] = []
    filings: list[Filing] = []


def pad_cik(cik: str | int) -> str:
    return str(int(cik)).zfill(10)


class EdgarClient:
    def __init__(self, settings: Settings, cache: ResponseCache | None = None, client: HttpClient | None = None):
        ua = settings.sec_user_agent.strip()
        if "@" not in ua:
            raise ProviderError(PROVIDER, "SEC_USER_AGENT must include a contact email (SEC fair-access policy).")
        self.client = client or HttpClient(
            PROVIDER,
            headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
            rate_limiter=RateLimiter(max(1, int(settings.sec_requests_per_second)), 1.0),
            timeout=settings.sec_timeout_seconds,
            cache=cache,
        )
        self._ticker_map: dict[str, TickerIdentity] | None = None

    def ticker_map(self) -> dict[str, TickerIdentity]:
        if self._ticker_map is None:
            data = self.client.get_json(TICKERS_URL, cache_ttl=timedelta(days=1))
            self._ticker_map = parse_ticker_map(data)
        return self._ticker_map

    def resolve_ticker(self, ticker: str) -> TickerIdentity:
        t = normalize_ticker(ticker)
        ident = self.ticker_map().get(t)
        if not ident:
            raise DataUnavailable(PROVIDER, f"{t} is not in SEC's ticker list (not an SEC registrant or delisted).")
        return ident

    def submissions(self, cik: str) -> CompanySubmissions:
        data = self.client.get_json(SUBMISSIONS_URL.format(cik=pad_cik(cik)), cache_ttl=timedelta(hours=6))
        return parse_submissions(data)

    def companyfacts(self, cik: str) -> dict[str, Any]:
        return self.client.get_json(COMPANYFACTS_URL.format(cik=pad_cik(cik)), cache_ttl=timedelta(hours=12))


def normalize_ticker(ticker: str) -> str:
    # SEC uses '-' for share classes (BRK-B); vendors often use '.' (BRK.B)
    return ticker.strip().upper().replace(".", "-")


def parse_ticker_map(data: dict[str, Any]) -> dict[str, TickerIdentity]:
    out: dict[str, TickerIdentity] = {}
    if "fields" in data and "data" in data:  # company_tickers_exchange.json
        idx = {f: i for i, f in enumerate(data["fields"])}
        for row in data["data"]:
            t = str(row[idx["ticker"]]).upper()
            out.setdefault(t, TickerIdentity(ticker=t, cik=pad_cik(row[idx["cik"]]), name=row[idx["name"]],
                                             exchange=row[idx["exchange"]] if "exchange" in idx else None))
    else:  # company_tickers.json
        for row in data.values():
            t = str(row["ticker"]).upper()
            out.setdefault(t, TickerIdentity(ticker=t, cik=pad_cik(row["cik_str"]), name=row["title"]))
    return out


def parse_submissions(data: dict[str, Any]) -> CompanySubmissions:
    cik = pad_cik(data["cik"])
    recent = data.get("filings", {}).get("recent", {})
    filings: list[Filing] = []
    n = len(recent.get("accessionNumber", []))
    for i in range(n):
        accn = recent["accessionNumber"][i]
        doc = (recent.get("primaryDocument") or [None] * n)[i]
        rd = (recent.get("reportDate") or [None] * n)[i]
        url = ARCHIVE_URL.format(cik_int=int(cik), accn_nodash=accn.replace("-", ""), doc=doc or "")
        filings.append(Filing(
            accession=accn, form=recent["form"][i], filed_date=date.fromisoformat(recent["filingDate"][i]),
            report_date=date.fromisoformat(rd) if rd else None, primary_document=doc,
            items=(recent.get("items") or [None] * n)[i] or None, url=url,
        ))
    return CompanySubmissions(
        cik=cik, name=data.get("name", ""), sic=data.get("sic") or None,
        sic_description=data.get("sicDescription") or None, fiscal_year_end=data.get("fiscalYearEnd") or None,
        exchanges=[e for e in data.get("exchanges", []) if e], tickers=data.get("tickers", []), filings=filings,
    )


# Plain-English labels for common 8-K items (https://www.sec.gov/files/form8-k.pdf)
EIGHT_K_ITEMS = {
    "1.01": "Entry into a material definitive agreement",
    "1.02": "Termination of a material definitive agreement",
    "1.03": "Bankruptcy or receivership",
    "2.01": "Completion of acquisition or disposition of assets",
    "2.02": "Results of operations and financial condition (earnings)",
    "2.03": "Creation of a direct financial obligation",
    "2.05": "Costs associated with exit or disposal activities",
    "2.06": "Material impairments",
    "3.01": "Notice of delisting or failure to satisfy listing rule",
    "4.02": "Non-reliance on previously issued financial statements",
    "5.02": "Departure/appointment of directors or officers",
    "5.07": "Submission of matters to a vote of security holders",
    "7.01": "Regulation FD disclosure",
    "8.01": "Other events",
    "9.01": "Financial statements and exhibits",
}


def describe_8k_items(items: str | None) -> list[str]:
    if not items:
        return []
    return [f"Item {i.strip()}: {EIGHT_K_ITEMS.get(i.strip(), 'Other')}" for i in items.split(",") if i.strip()]
