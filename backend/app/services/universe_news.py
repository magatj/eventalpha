"""Company-specific news and filings for the WHOLE universe, independent of analysis.

* Market-wide news (one Massive request, ~1000 ticker-tagged articles covering ~2–4 days) is
  linked to every universe company it mentions; it also records that each company's news was
  checked over that window (coverage), even when nothing was found.
* A small rotation of per-ticker news requests fills gaps for the companies checked least
  recently (bounded per run to respect the 5 requests/minute free tier).
* SEC filings and estimated earnings dates (from 8-K Item 2.02 cadence) for every universe
  company — SEC requests are cheap (no Massive quota) and cached.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import nulls_first, select
from sqlalchemy.orm import Session

from app.config import DataMode, get_settings
from app.models import Company, CompanyBaseline
from app.providers.news.base import RawArticle
from app.providers.news.live import economic_calendar, estimate_next_earnings, filings_to_articles
from app.services.baseline import get_baseline
from app.services.events import ingest_articles, upsert_upcoming
from app.services.forecasting.sectors import sector_from_sic
from app.services.http import ProviderError, ProviderNotConfigured
from app.services.providers import edgar_client, market_provider, news_provider


def _raw(a) -> RawArticle:  # ProviderArticle -> RawArticle
    return RawArticle(url=a.url, title=a.title, summary=a.summary, source_name=a.source_name,
                      published_at=a.published_at, tickers=a.tickers, provider=a.provider)


def ingest_universe_news(session: Session, mode: DataMode, companies: dict[str, Company],
                         rotate_n: int | None = None, progress: Callable[[str], None] = lambda m: None,
                         resolver=None, known_tickers: set[str] | None = None) -> dict[str, Any]:
    s = get_settings()
    rotate_n = s.news_rotation_per_run if rotate_n is None else rotate_n
    stats: dict[str, Any] = {"errors": []}
    now = datetime.now(UTC)
    try:
        mp = market_provider(mode)
    except ProviderNotConfigured as exc:
        mp = None
        stats["errors"].append(exc.message)

    if mp is not None:
        progress("Market-wide news")
        try:
            arts = mp.get_market_news()
            # resolver/known_tickers let market-wide news introduce stocks outside the monitored set
            st = ingest_articles(session, mode, [_raw(a) for a in arts], dict(companies), resolver, known_tickers)
            stats["market_wide"] = {"articles": len(arts), **st}
            dated = [a.published_at for a in arts if a.published_at]
            if dated:
                window_start = min(dated)
                for c in companies.values():
                    b = get_baseline(session, c)
                    b.news_checked_at, b.news_window_start = now, window_start
                stats["market_window_hours"] = round((now - window_start).total_seconds() / 3600, 1)
        except ProviderError as exc:
            stats["errors"].append(f"market-wide news: {exc.message}")
        session.commit()

        # Per-ticker rotation: least recently checked first (never-checked first).
        ids = {c.id: t for t, c in companies.items()}
        rows = session.scalars(select(CompanyBaseline).where(CompanyBaseline.company_id.in_(ids))
                               .order_by(nulls_first(CompanyBaseline.ticker_news_checked_at.asc())).limit(rotate_n))
        rotated = []
        for b in list(rows):
            t = ids[b.company_id]
            progress(f"Company news {t}")
            try:
                ingest_articles(session, mode, [_raw(a) for a in mp.get_company_news(t)], {t: companies[t]})
                b.ticker_news_checked_at = now
                rotated.append(t)
            except ProviderError as exc:
                stats["errors"].append(f"news {t}: {exc.message}")
            session.commit()
        stats["rotated"] = rotated

    # Macro RSS + economic calendar (not company-specific).
    prov = news_provider(mode, list(companies))
    stats["rss"] = ingest_articles(session, mode, prov.fetch_recent_news(), companies)
    upsert_upcoming(session, mode, economic_calendar(date.today()) if mode == "live"
                    else prov.fetch_upcoming_events([]), companies)
    stats["errors"].extend(getattr(prov, "errors", [])[:10])
    session.commit()
    return stats


def ingest_universe_filings(session: Session, mode: DataMode, companies: dict[str, Company],
                            days: int = 30, progress: Callable[[str], None] = lambda m: None) -> dict[str, Any]:
    """SEC filings + estimated earnings dates for every universe company."""
    now = datetime.now(UTC)
    stats: dict[str, Any] = {"companies": 0, "articles": 0, "upcoming": 0, "errors": []}
    if mode == "demo":
        prov = news_provider(mode, list(companies))
        stats["upcoming"] = upsert_upcoming(session, mode, prov.fetch_upcoming_events(list(companies)), companies)
        for c in companies.values():
            get_baseline(session, c).filings_checked_at = now
        session.commit()
        return stats
    edgar = edgar_client()
    since = date.today() - timedelta(days=days)
    for n, (t, c) in enumerate(companies.items(), 1):
        if n % 20 == 1:
            progress(f"SEC filings ({n}/{len(companies)})")
        try:
            cik = c.cik or edgar.resolve_ticker(t).cik
            subs = edgar.submissions(cik)
            c.cik = c.cik or cik
            if subs.sic:  # classify every universe company, not only analyzed ones
                c.sic_code, c.industry = subs.sic, subs.sic_description
                c.sector = sector_from_sic(subs.sic, subs.sic_description)
            st = ingest_articles(session, mode, filings_to_articles(subs, t, since), {t: c})
            ev = estimate_next_earnings(subs, date.today())
            if ev:
                ev.tickers = [t]
                stats["upcoming"] += upsert_upcoming(session, mode, [ev], {t: c})
            stats["articles"] += st["new_articles"]
            get_baseline(session, c).filings_checked_at = now
            stats["companies"] += 1
        except ProviderError as exc:
            stats["errors"].append(f"{t}: {exc.message}")
        session.commit()
    return stats
