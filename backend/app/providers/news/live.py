"""Live news/event providers: SEC filings, Massive news API, RSS, calendars."""

from __future__ import annotations

import json
import statistics
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from app.providers.market.base import MarketDataProvider
from app.providers.news.base import NewsProvider, RawArticle, UpcomingEvent
from app.providers.news.rss import RssNewsProvider
from app.providers.sec.edgar import MATERIAL_FORMS, CompanySubmissions, EdgarClient, describe_8k_items
from app.services.http import ProviderError

CALENDAR_FILE = Path(__file__).resolve().parents[2] / "data" / "economic_calendar.json"


def filings_to_articles(subs: CompanySubmissions, ticker: str, since: date) -> list[RawArticle]:
    out = []
    for f in subs.filings:
        if f.filed_date < since or f.form not in MATERIAL_FORMS:
            continue
        items = describe_8k_items(f.items)
        title = f"{subs.name} filed {f.form}" + (f" — {items[0].split(': ', 1)[1]}" if items else "")
        out.append(RawArticle(
            url=f.url, title=title, summary="; ".join(items) or None, source_name="SEC EDGAR",
            published_at=datetime.combine(f.filed_date, time(0, 0), tzinfo=UTC),
            tickers=[ticker], provider="sec_edgar", kind="sec_filing",
        ))
    return out


def estimate_next_earnings(subs: CompanySubmissions, today: date) -> UpcomingEvent | None:
    """Estimate the next earnings release from the cadence of past 8-K Item 2.02 filings.

    This is an *estimate* (status="estimated"), not a company-confirmed date."""
    dates = sorted({f.filed_date for f in subs.filings if f.form == "8-K" and f.items and "2.02" in f.items})
    if len(dates) < 3:
        return None
    gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)][-6:]
    gap = statistics.median(gaps)
    if not 70 <= gap <= 120:
        return None
    nxt = dates[-1] + timedelta(days=round(gap))
    while nxt < today:
        nxt += timedelta(days=round(gap))
    return UpcomingEvent(
        title=f"{subs.name}: estimated next earnings release", event_type="earnings", status="estimated",
        event_date=nxt, tickers=[t.upper() for t in subs.tickers[:1]],
        url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={subs.cik}&type=8-K",
        source_name="Estimated from SEC 8-K Item 2.02 filing cadence",
        detail=f"Median interval between recent earnings 8-Ks: {gap:.0f} days; last on {dates[-1]}. Not confirmed by the company.",
    )


def economic_calendar(today: date, horizon_days: int = 120) -> list[UpcomingEvent]:
    data = json.loads(CALENDAR_FILE.read_text())
    out = []
    for e in data["events"]:
        d = date.fromisoformat(e["date"])
        if today <= d <= today + timedelta(days=horizon_days):
            out.append(UpcomingEvent(title=e["title"], event_type="economic_release", status=e["status"], event_date=d,
                                     url=data["_source"], source_name="Federal Reserve", detail=data["_note"]))
    return out


class LiveNewsProvider(NewsProvider):
    name = "live"

    def __init__(self, edgar: EdgarClient | None, market: MarketDataProvider | None, rss: RssNewsProvider | None):
        self.edgar = edgar
        self.market = market
        self.rss = rss
        self.errors: list[str] = []

    def fetch_recent_news(self) -> list[RawArticle]:
        if not self.rss:
            return []
        items = self.rss.fetch_recent_news()
        self.errors.extend(self.rss.errors)
        return items

    def fetch_company_news(self, ticker: str) -> list[RawArticle]:
        if not self.market:
            return []
        try:
            return [RawArticle(url=a.url, title=a.title, summary=a.summary, source_name=a.source_name,
                               published_at=a.published_at, tickers=a.tickers, provider=a.provider)
                    for a in self.market.get_company_news(ticker)]
        except ProviderError as exc:
            self.errors.append(f"company news {ticker}: {exc.message}")
            return []

    def fetch_sec_filings(self, ticker: str, days: int = 60) -> list[RawArticle]:
        if not self.edgar:
            return []
        try:
            ident = self.edgar.resolve_ticker(ticker)
            subs = self.edgar.submissions(ident.cik)
            return filings_to_articles(subs, ticker, date.today() - timedelta(days=days))
        except ProviderError as exc:
            self.errors.append(f"SEC filings {ticker}: {exc.message}")
            return []

    def fetch_upcoming_events(self, tickers: list[str]) -> list[UpcomingEvent]:
        today = date.today()
        out = economic_calendar(today)
        if not self.edgar:
            return out
        for t in tickers:
            try:
                subs = self.edgar.submissions(self.edgar.resolve_ticker(t).cik)
                ev = estimate_next_earnings(subs, today)
                if ev:
                    ev.tickers = [t]
                    out.append(ev)
            except ProviderError as exc:
                self.errors.append(f"calendar {t}: {exc.message}")
        return out
