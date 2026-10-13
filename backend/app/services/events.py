"""Event detection: URL canonicalization, deduplication, rumor detection, classification,
impact heuristics, company association and persistence.

Heuristic outputs are labeled as such; the AI layer may refine them later. Reports that rely
on anonymous sources ("reportedly", "people familiar") are stored as status="unconfirmed".
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, Event, EventCompanyRelationship, NewsArticle
from app.providers.news.base import RawArticle, UpcomingEvent

STOPWORDS = set(
    "a an the and or of to in on for with by at from as is are was were be been it its this that after over into "
    "says said report reports update new demo inc corp co ltd plc llc company shares stock stocks".split()
)
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|cmpid|ito$)")

RUMOR_PATTERNS = re.compile(
    r"\b(reportedly|rumou?r(?:ed|s)?|sources say|people familiar|according to (?:people|sources)|"
    r"is said to|in talks|considering|weighs|exploring|could be|may be|speculat\w+|unconfirmed)\b",
    re.I,
)

EVENT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("earnings", re.compile(r"\b(earnings|quarterly results|results of operations|q[1-4] results|eps|revenue (?:rose|fell)|item 2\.02)\b", re.I)),
    ("guidance", re.compile(r"\b(guidance|outlook|forecast(?:s)? (?:raised|cut|lowered)|raises|lowers|reaffirms)\b", re.I)),
    ("m_and_a", re.compile(r"\b(acquir\w+|acquisition|merger|merge|takeover|buyout|divest\w*|spin-?off|item 2\.01)\b", re.I)),
    ("product_launch", re.compile(r"\b(launch\w*|unveil\w*|introduc\w+|release[sd]? (?:new|its)|product line|debuts?)\b", re.I)),
    ("regulatory", re.compile(r"\b(fda|ftc|doj|antitrust|regulat\w+|approval|approved|sanction\w*|tariff\w*|export control|probe|investigation)\b", re.I)),
    ("legal", re.compile(r"\b(lawsuit|sued|settle\w*|recall\w*|litigation|verdict|fine[sd]?)\b", re.I)),
    ("economic_release", re.compile(r"\b(fomc|federal reserve|interest rates?|cpi|inflation|payrolls|employment situation|gdp|consumer price|producer price|jobless)\b", re.I)),
    ("capital_return", re.compile(r"\b(buyback|repurchase|dividend)\b", re.I)),
    ("management", re.compile(r"\b(ceo|cfo|chief executive|resign\w*|appoint\w*|steps down|item 5\.02)\b", re.I)),
    ("contract", re.compile(r"\b(contract|agreement|partnership|deal with|supply agreement|item 1\.01)\b", re.I)),
]

POSITIVE = re.compile(r"\b(beat|beats|raises|raised|record|surge\w*|soar\w*|jump\w*|win[s]?|wins|approval|approved|expand\w*|upgrade\w*|strong|growth|accelerat\w*|partnership|contract award)\b", re.I)
NEGATIVE = re.compile(r"\b(miss(?:es|ed)?|cut[s]?|lower(?:s|ed)|recall\w*|lawsuit|probe|investigation|downgrade\w*|plunge\w*|slump\w*|fall[s]?|fell|weak|delay\w*|layoff\w*|impairment|non-reliance|delist\w*|bankrupt\w*|fine[sd]?|sanction\w*)\b", re.I)

EXCHANGE_TICKER = re.compile(r"\((?:NYSE|NASDAQ|Nasdaq|NYSE American|NYSE Arca|Cboe|AMEX)\s*:\s*([A-Z]{1,5}(?:\.[A-Z])?)\)")
CASHTAG = re.compile(r"(?<![A-Za-z0-9])\$([A-Z]{1,5})(?![A-Za-z])")
NAME_SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|holdings?|group|n\.?v\.?|s\.?a\.?|the)\b\.?", re.I)


def canonicalize_url(url: str) -> str:
    p = urlparse(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not TRACKING_PARAMS.match(k.lower())]
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower() or "https", p.netloc.lower().removeprefix("www."), path, "", urlencode(sorted(q)), ""))


def title_tokens(title: str) -> set[str]:
    t = re.sub(r"\[demo\]", " ", title.lower())
    t = re.sub(r"\s[-|–—]\s[^-|–—]{2,40}$", " ", t)  # strip " - Publisher" suffix
    words = re.findall(r"[a-z0-9$%.]+", t)
    return {w.strip(".") for w in words if w.strip(".") and w.strip(".") not in STOPWORDS and len(w.strip(".")) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def same_event(a_title: str, b_title: str, a_time: datetime | None, b_time: datetime | None,
               threshold: float = 0.55, window: timedelta = timedelta(hours=72)) -> bool:
    if a_time and b_time and abs(a_time - b_time) > window:
        return False
    return jaccard(title_tokens(a_title), title_tokens(b_title)) >= threshold


def is_rumor(title: str, summary: str | None = None) -> bool:
    return bool(RUMOR_PATTERNS.search(f"{title} {summary or ''}"))


def classify(title: str, summary: str | None = None, kind: str = "news") -> str:
    text = f"{title} {summary or ''}"
    for etype, rx in EVENT_RULES:
        if rx.search(text):
            return etype
    if kind == "sec_filing":
        return "sec_filing"
    if kind == "economic":
        return "economic_release"
    return "industry" if kind == "news" else kind


def heuristic_impact(title: str, summary: str | None = None) -> tuple[str, str]:
    text = f"{title} {summary or ''}"
    pos = len(POSITIVE.findall(text))
    neg = len(NEGATIVE.findall(text))
    if pos > neg:
        return "positive", f"Keyword heuristic: {pos} positive vs {neg} negative signals in headline/summary."
    if neg > pos:
        return "negative", f"Keyword heuristic: {neg} negative vs {pos} positive signals in headline/summary."
    return "uncertain", "No clear directional signal in the headline/summary (keyword heuristic)."


def clean_company_name(name: str) -> str:
    n = NAME_SUFFIX.sub(" ", name.replace(",", " "))
    return re.sub(r"\s+", " ", n).strip()


@dataclass
class TickerMatch:
    ticker: str
    confidence: float
    method: str


def extract_tickers(text: str, provider_tags: list[str], names: dict[str, str]) -> list[TickerMatch]:
    """Associate an article with tickers in the monitored universe.

    names: ticker -> company name. Confidence reflects how explicit the link is."""
    found: dict[str, TickerMatch] = {}

    def add(t: str, c: float, m: str) -> None:
        t = t.upper()
        if t in names and (t not in found or found[t].confidence < c):
            found[t] = TickerMatch(t, c, m)

    for m in EXCHANGE_TICKER.findall(text):
        add(m.replace(".", "-"), 0.95, "exchange_ticker")
    for t in provider_tags:
        add(t, 0.85, "provider_tag")
    for m in CASHTAG.findall(text):
        add(m, 0.8, "cashtag")
    low = text.lower()
    for t, name in names.items():
        cleaned = clean_company_name(name or "")
        if len(cleaned) >= 4 and re.search(rf"\b{re.escape(cleaned.lower())}\b", low):
            add(t, 0.6, "name_match")
    return sorted(found.values(), key=lambda m: -m.confidence)


def content_hash(title: str, summary: str | None) -> str:
    return hashlib.sha256(f"{title}|{summary or ''}".encode()).hexdigest()


def materiality_score(event_type: str, kind: str, status: str, article_count: int) -> float:
    base = {
        "earnings": 0.8, "guidance": 0.85, "m_and_a": 0.8, "regulatory": 0.6, "legal": 0.5,
        "product_launch": 0.5, "contract": 0.55, "capital_return": 0.4, "management": 0.45,
        "economic_release": 0.5, "sec_filing": 0.4, "industry": 0.3,
    }.get(event_type, 0.3)
    if status == "unconfirmed":
        base *= 0.5
    if kind == "sec_filing":
        base = min(1.0, base + 0.1)  # primary source
    return round(min(1.0, base + 0.05 * min(article_count - 1, 4)), 2)


# ---------------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------------
def ingest_articles(session: Session, data_mode: str, articles: list[RawArticle],
                    companies: dict[str, Company]) -> dict[str, int]:
    """Store new articles, merge duplicates into events and link companies.

    Unchanged articles (same canonical URL + content hash) are skipped, so repeated runs
    don't reprocess them."""
    stats = {"new_articles": 0, "skipped": 0, "new_events": 0, "merged": 0, "links": 0}
    names = {t: (c.name or "") for t, c in companies.items()}
    since = datetime.now(UTC) - timedelta(days=4)
    recent_events = list(session.scalars(
        select(Event).where(Event.data_mode == data_mode, Event.last_published_at >= since, Event.status != "scheduled")
    ))

    for a in sorted(articles, key=lambda x: x.published_at or datetime.min.replace(tzinfo=UTC)):
        curl = canonicalize_url(a.url)
        chash = content_hash(a.title, a.summary)
        existing = session.scalar(select(NewsArticle).where(NewsArticle.data_mode == data_mode,
                                                            NewsArticle.canonical_url == curl))
        if existing is not None:
            if existing.content_hash == chash:
                stats["skipped"] += 1
                continue
            existing.title, existing.summary, existing.content_hash = a.title, a.summary, chash
            continue

        rumor = is_rumor(a.title, a.summary)
        etype = classify(a.title, a.summary, a.kind)
        impact, why = heuristic_impact(a.title, a.summary)
        ev = next((e for e in recent_events if same_event(e.title, a.title, e.last_published_at, a.published_at)), None)
        if ev is None:
            ev = Event(
                data_mode=data_mode, fingerprint=hashlib.sha256(curl.encode()).hexdigest()[:40], title=a.title,
                summary=a.summary, event_type=etype, status="unconfirmed" if rumor else "confirmed",
                impact=impact, impact_explanation=why, source_name=a.source_name, primary_url=a.url,
                first_published_at=a.published_at, last_published_at=a.published_at, article_count=1,
                content_hash=chash,
            )
            session.add(ev)
            session.flush()
            recent_events.append(ev)
            stats["new_events"] += 1
        else:
            ev.article_count += 1
            if a.published_at and (ev.last_published_at is None or a.published_at > ev.last_published_at):
                ev.last_published_at = a.published_at
            # A confirming non-rumor report (or primary source) upgrades an unconfirmed event.
            if ev.status == "unconfirmed" and not rumor:
                ev.status = "confirmed"
            if a.kind == "sec_filing":
                ev.primary_url, ev.source_name = a.url, a.source_name
            stats["merged"] += 1
        ev.materiality = materiality_score(ev.event_type, a.kind, ev.status, ev.article_count)

        session.add(NewsArticle(
            data_mode=data_mode, canonical_url=curl, url=a.url, title=a.title, summary=a.summary,
            source_name=a.source_name, provider=a.provider, published_at=a.published_at, content_hash=chash,
            tickers=a.tickers, is_rumor=rumor, event_id=ev.id,
        ))
        stats["new_articles"] += 1

        for m in extract_tickers(f"{a.title} {a.summary or ''}", a.tickers, names):
            comp = companies[m.ticker]
            link = session.scalar(select(EventCompanyRelationship).where(
                EventCompanyRelationship.event_id == ev.id, EventCompanyRelationship.company_id == comp.id))
            if link is None:
                session.add(EventCompanyRelationship(event_id=ev.id, company_id=comp.id, confidence=m.confidence,
                                                     method=m.method, impact=ev.impact,
                                                     explanation="Linked by " + m.method.replace("_", " ")))
                stats["links"] += 1
            elif m.confidence > link.confidence:
                link.confidence, link.method = m.confidence, m.method
        session.flush()
    return stats


def upsert_upcoming(session: Session, data_mode: str, items: list[UpcomingEvent],
                    companies: dict[str, Company]) -> int:
    n = 0
    for u in items:
        fp = hashlib.sha256(f"{u.event_type}|{u.title}|{','.join(sorted(u.tickers))}".encode()).hexdigest()[:40]
        ev = session.scalar(select(Event).where(Event.data_mode == data_mode, Event.fingerprint == fp))
        if ev is None:
            ev = Event(data_mode=data_mode, fingerprint=fp, title=u.title, summary=u.detail, event_type=u.event_type,
                       status=u.status, impact="uncertain",
                       impact_explanation="Scheduled/estimated future event; direction unknown until it occurs.",
                       source_name=u.source_name, primary_url=u.url, event_date=u.event_date, article_count=1,
                       first_published_at=datetime.now(UTC), last_published_at=datetime.now(UTC),
                       materiality=0.6 if u.event_type == "earnings" else 0.5)
            session.add(ev)
            session.flush()
            n += 1
        else:
            ev.event_date, ev.summary = u.event_date, u.detail
        for t in u.tickers:
            comp = companies.get(t)
            if comp and not session.scalar(select(EventCompanyRelationship).where(
                    EventCompanyRelationship.event_id == ev.id, EventCompanyRelationship.company_id == comp.id)):
                session.add(EventCompanyRelationship(event_id=ev.id, company_id=comp.id, confidence=0.9,
                                                     method="sec_cik" if "SEC" in u.source_name else "calendar",
                                                     impact="uncertain", explanation="Company-specific scheduled event"))
    session.flush()
    return n


def upcoming_for_company(session: Session, company_id: int, today: date | None = None) -> list[Event]:
    today = today or date.today()
    return list(session.scalars(
        select(Event).join(EventCompanyRelationship).where(
            EventCompanyRelationship.company_id == company_id, Event.event_date >= today
        ).order_by(Event.event_date)
    ))
