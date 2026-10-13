"""News/event provider interface."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class RawArticle(BaseModel):
    url: str
    title: str
    summary: str | None = None
    source_name: str | None = None
    published_at: datetime | None = None
    tickers: list[str] = Field(default_factory=list)  # tickers tagged by the provider (if any)
    provider: str
    kind: str = "news"  # news | sec_filing | press_release | economic


class UpcomingEvent(BaseModel):
    title: str
    event_type: str
    status: str  # scheduled (officially published) | estimated (inferred, not confirmed)
    event_date: date
    tickers: list[str] = Field(default_factory=list)
    url: str
    source_name: str
    detail: str | None = None


class NewsProvider:
    """Base class; providers override the methods they support (defaults return nothing)."""

    name: str = "abstract"

    def fetch_recent_news(self) -> list[RawArticle]:
        return []

    def fetch_company_news(self, ticker: str) -> list[RawArticle]:
        return []

    def fetch_upcoming_events(self, tickers: list[str]) -> list[UpcomingEvent]:
        return []

    def fetch_sec_filings(self, ticker: str) -> list[RawArticle]:
        return []
