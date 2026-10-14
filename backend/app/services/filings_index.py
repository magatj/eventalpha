"""Market-wide SEC filing detection from EDGAR daily form indexes.

One small file per business day lists every filing (form, filer CIK, path). We turn 8-K /
10-Q / 10-K filings by securities in the master universe into events — so a filing can put a
stock in front of the discovery engine even if it was never monitored. For 8-Ks of eligible
companies, the item numbers (e.g. 2.02 earnings, 1.01 material agreement) are looked up from the
filer's submissions, bounded per run. Days are recorded in filing_index_days (idempotent).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import DataMode, get_settings
from app.models import FilingIndexDay, Security
from app.providers.news.base import RawArticle
from app.providers.sec.edgar import SUBMISSIONS_URL, daily_index, describe_8k_items, pad_cik
from app.services.events import ingest_articles
from app.services.http import ProviderError
from app.services.master_universe import ADR_TYPES, PIPELINE_TYPES, company_for_security

FORMS = {"8-K", "10-K", "10-Q", "8-K/A"}
MAX_ITEM_LOOKUPS = 200


def ingest_filing_indexes(session: Session, mode: DataMode, progress: Callable[[str], None] = lambda m: None,
                          today: date | None = None) -> dict[str, Any]:
    stats: dict[str, Any] = {"days": [], "filings_matched": 0, "events": 0, "item_lookups": 0, "errors": []}
    if mode == "demo":
        stats["skipped"] = "demo mode has no SEC index"
        return stats
    from app.services.providers import edgar_client

    client = edgar_client()
    today = today or date.today()
    done = {d for (d,) in session.execute(select(FilingIndexDay.day)).all()}
    days = [today - timedelta(days=k) for k in range(get_settings().filing_index_lookback_days)]
    days = [d for d in days if d.weekday() < 5 and d not in done and d < today]  # today's index is incomplete
    by_cik: dict[str, Security] = {}
    for sec in session.scalars(select(Security).where(Security.data_mode == mode, Security.active,
                                                      Security.security_type.in_(PIPELINE_TYPES | ADR_TYPES),
                                                      Security.cik.is_not(None))):
        by_cik.setdefault(sec.cik, sec)  # one primary listing per filer
    lookups = 0
    for d in sorted(days):
        progress(f"SEC daily index {d}")
        try:
            filings = daily_index(client, d)
        except ProviderError as exc:
            stats["errors"].append(f"{d}: {exc.message}")
            continue
        if filings is None:
            session.add(FilingIndexDay(day=d, status="no_index", filings=0))
            session.commit()
            continue
        articles: list[tuple[RawArticle, Security]] = []
        for f in filings:
            if f.form not in FORMS or f.cik not in by_cik:
                continue
            sec = by_cik[f.cik]
            title = f"{sec.name or f.company} filed {f.form}"
            summary = None
            if f.form.startswith("8-K") and sec.eligibility_status in ("eligible", "catalyst_eligible") \
                    and lookups < MAX_ITEM_LOOKUPS:
                items = _items_for(client, f.cik, f.path)
                lookups += 1
                if items:
                    desc = describe_8k_items(items)
                    title += " — " + desc[0].split(": ", 1)[1]
                    summary = "; ".join(desc)
            articles.append((RawArticle(
                url=f.url, title=title, summary=summary, source_name="SEC EDGAR",
                published_at=datetime.combine(f.filed_date, time(21, 0), tzinfo=UTC), tickers=[sec.ticker],
                provider="sec_edgar_index", kind="sec_filing"), sec))
        for art, sec in articles:
            comp = company_for_security(session, sec)
            st = ingest_articles(session, mode, [art], {sec.ticker: comp})
            stats["events"] += st["new_events"]
        stats["filings_matched"] += len(articles)
        session.add(FilingIndexDay(day=d, status="ingested", filings=len(articles)))
        session.commit()
        stats["days"].append(str(d))
    stats["item_lookups"] = lookups
    return stats


def _items_for(client, cik: str, path: str) -> str | None:
    """8-K item numbers for one accession, from the filer's recent-filings list."""
    accn = path.rsplit("/", 1)[-1].removesuffix(".txt")
    try:
        data = client.client.get_json(SUBMISSIONS_URL.format(cik=pad_cik(cik)))
    except ProviderError:
        return None
    recent = data.get("filings", {}).get("recent", {})
    for i, a in enumerate(recent.get("accessionNumber", [])):
        if a == accn:
            return (recent.get("items") or [None] * (i + 1))[i] or None
    return None
