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

# Order matters: the first matching category wins (most specific first).
EVENT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("guidance", re.compile(r"\b(guidance|outlook|forecast(?:s)? (?:raised|cut|lowered)|raises (?:full-year|annual|its)|lowers (?:full-year|annual|its)|reaffirms)\b", re.I)),
    ("earnings_surprise", re.compile(r"\b(beats?|miss(?:es|ed)?|tops?|surpass\w*|falls? short of) (?:\w+ ){0,3}(?:estimates|expectations|consensus|forecasts)\b", re.I)),
    ("m_and_a", re.compile(r"\b(acquir\w+|acquisition|merger|merge|takeover|buyout|divest\w*|spin-?off|tender offer|item 2\.01)\b", re.I)),
    ("regulatory", re.compile(r"\b(fda|ftc|doj|sec charges|antitrust|regulat\w+|approval|approved|clearance|sanction\w*|tariff\w*|export control|probe|investigation)\b", re.I)),
    ("financing", re.compile(r"\b(offering|senior notes|convertible|credit facility|private placement|at-the-market|debt financing|refinanc\w+|raises \$|item 2\.03)\b", re.I)),
    ("customer_agreement", re.compile(r"\b(selected by|chosen by|purchase order|multi-year (?:deal|agreement|contract)|supply agreement with|signs? (?:a )?deal with)\b", re.I)),
    ("contract", re.compile(r"\b(contract|awarded|award of|item 1\.01)\b", re.I)),
    ("partnership", re.compile(r"\b(partnership|partners with|collaborat\w+|alliance|joint venture|teams up)\b", re.I)),
    ("capital_investment", re.compile(r"\b(invest(?:s|ing|ment of)? \$|new (?:plant|factory|facility|fab)|expand\w* capacity|capital expenditure|build\w* (?:a )?(?:new )?(?:plant|data cent(?:er|re)s?))\b", re.I)),
    ("supply_chain", re.compile(r"\b(supply chain|supplier|shortage|disruption|inventory glut|component costs?)\b", re.I)),
    ("product_launch", re.compile(r"\b(launch\w*|unveil\w*|introduc\w+|release[sd]? (?:new|its)|product line|debuts?)\b", re.I)),
    ("legal", re.compile(r"\b(lawsuit|sued|settle\w*|recall\w*|litigation|verdict|fine[sd]?|class action)\b", re.I)),
    ("economic_release", re.compile(r"\b(fomc|federal reserve|interest rates?|cpi|inflation|payrolls|employment situation|gdp|consumer price|producer price|jobless)\b", re.I)),
    ("capital_return", re.compile(r"\b(buyback|repurchase|dividend)\b", re.I)),
    ("management", re.compile(r"\b(ceo|cfo|chief executive|resign\w*|appoint\w*|steps down|item 5\.02)\b", re.I)),
    ("earnings", re.compile(r"\b(earnings|quarterly results|results of operations|q[1-4] results|eps|revenue (?:rose|fell)|item 2\.02)\b", re.I)),
    ("industry", re.compile(r"\b(industry|sector|demand for|market for)\b", re.I)),
]

ANALYST = re.compile(r"\b(analysts?|upgrade[sd]?|downgrade[sd]?|price target|rating|initiat\w+ coverage|outperform|underperform|overweight|underweight)\b", re.I)
MANAGEMENT = re.compile(r"\b(expects?|outlook|guidance|forecasts?|anticipates?|ceo says|cfo says|management said|reaffirms)\b", re.I)
AMOUNT = re.compile(r"\$\s?(\d+(?:\.\d+)?)\s?(billion|bn|million|mn|m|b)\b", re.I)
PRIMARY_SOURCES = ("sec edgar", "business wire", "pr newswire", "globenewswire", "accesswire", "investor relations")
MAJOR_OUTLETS = ("reuters", "bloomberg", "associated press", "wall street journal", "financial times", "cnbc",
                 "barron", "marketwatch", "the motley fool", "zacks", "benzinga", "seeking alpha", "investing.com")

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


def extract_tickers(text: str, provider_tags: list[str], names: dict[str, str],
                    known_tickers: set[str] | None = None) -> list[TickerMatch]:
    """Associate an article with tickers.

    names: monitored ticker -> company name (also matched by name). known_tickers: every listed
    ticker in the master universe, matched only through explicit signals (exchange tag, provider
    tag, cashtag); this is how news introduces stocks that were never monitored."""
    found: dict[str, TickerMatch] = {}
    known = known_tickers or set()

    def add(t: str, c: float, m: str) -> None:
        t = t.upper()
        if (t in names or t in known) and (t not in found or found[t].confidence < c):
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


CATEGORY_BASE = {
    "guidance": 0.85, "earnings_surprise": 0.85, "m_and_a": 0.8, "earnings": 0.75, "regulatory": 0.65,
    "customer_agreement": 0.65, "contract": 0.6, "financing": 0.55, "capital_investment": 0.55,
    "partnership": 0.5, "product_launch": 0.5, "legal": 0.5, "supply_chain": 0.45, "economic_release": 0.5,
    "management": 0.45, "capital_return": 0.4, "sec_filing": 0.4, "industry": 0.3,
}
CLAIM_FACTOR = {"confirmed": 1.0, "scheduled": 1.0, "reported": 0.85, "management_statement": 0.9,
                "estimated": 0.8, "analyst_interpretation": 0.6, "speculation": 0.4}


def claim_type(title: str, summary: str | None, kind: str, rumor: bool) -> str:
    """Separate confirmed facts from management statements, analyst views and speculation."""
    text = f"{title} {summary or ''}"
    if rumor:
        return "speculation"
    if kind in ("sec_filing", "press_release"):
        return "confirmed"
    if ANALYST.search(text):
        return "analyst_interpretation"
    if MANAGEMENT.search(text):
        return "management_statement"
    return "reported"  # third-party report of a fact


def source_credibility(source_name: str | None, kind: str) -> float:
    s = (source_name or "").lower()
    if kind in ("sec_filing", "press_release") or any(p in s for p in PRIMARY_SOURCES):
        return 1.0
    if any(o in s for o in MAJOR_OUTLETS):
        return 0.85
    return 0.7 if s else 0.6


def parse_amount(text: str) -> float | None:
    best = None
    for num, unit in AMOUNT.findall(text):
        v = float(num) * (1e9 if unit.lower() in ("billion", "bn", "b") else 1e6)
        best = max(best or 0.0, v)
    return best


def materiality_score(event_type: str, kind: str, status: str, article_count: int,
                      claim: str | None = None, credibility: float | None = None) -> float:
    """Time-independent materiality: category x source credibility x confirmation, plus a small,
    capped corroboration bonus (article volume alone can't make an event material). Company
    relevance, financial significance, recency and timing are applied per company at selection
    time (see discovery.catalyst_score)."""
    return materiality_detail(event_type, kind, status, article_count, claim, credibility)["score"]


def materiality_detail(event_type: str, kind: str, status: str, article_count: int,
                       claim: str | None = None, credibility: float | None = None) -> dict:
    base = CATEGORY_BASE.get(event_type, 0.3)
    if claim is None:
        claim = "speculation" if status == "unconfirmed" else "confirmed" if kind == "sec_filing" else "reported"
    cred = credibility if credibility is not None else source_credibility(None, kind)
    conf = CLAIM_FACTOR.get(claim, 0.85)
    if status == "unconfirmed":
        conf = min(conf, 0.5)
    corroboration = 0.03 * min(max(article_count - 1, 0), 3)
    score = round(min(1.0, base * cred * conf + corroboration), 3)
    return {"score": score, "category": event_type, "category_base": base, "source_credibility": cred,
            "claim_type": claim, "confirmation_factor": conf, "corroboration_bonus": corroboration}


# ---------------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------------
def ingest_articles(session: Session, data_mode: str, articles: list[RawArticle],
                    companies: dict[str, Company], resolver=None,
                    known_tickers: set[str] | None = None) -> dict[str, int]:
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
        claim = claim_type(a.title, a.summary, a.kind, rumor)
        cred = source_credibility(a.source_name, a.kind)
        impact, why = heuristic_impact(a.title, a.summary)
        ev = next((e for e in recent_events if same_event(e.title, a.title, e.last_published_at, a.published_at)), None)
        if ev is None:
            ev = Event(
                data_mode=data_mode, fingerprint=hashlib.sha256(curl.encode()).hexdigest()[:40], title=a.title,
                summary=a.summary, event_type=etype, status="unconfirmed" if rumor else "confirmed",
                impact=impact, impact_explanation=why, source_name=a.source_name, primary_url=a.url,
                first_published_at=a.published_at, last_published_at=a.published_at, article_count=1,
                content_hash=chash, claim_type=claim, amount_usd=parse_amount(f"{a.title} {a.summary or ''}"),
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
            # the strongest claim and most credible source among clustered articles win
            if CLAIM_FACTOR.get(claim, 0) > CLAIM_FACTOR.get(ev.claim_type or "", 0):
                ev.claim_type = claim
            if a.kind == "sec_filing":
                ev.primary_url, ev.source_name = a.url, a.source_name
            stats["merged"] += 1
        prev_cred = (ev.materiality_detail or {}).get("source_credibility", 0)
        detail = materiality_detail(ev.event_type, a.kind, ev.status, ev.article_count, ev.claim_type,
                                    max(cred, prev_cred))
        ev.materiality, ev.materiality_detail = detail["score"], detail

        session.add(NewsArticle(
            data_mode=data_mode, canonical_url=curl, url=a.url, title=a.title, summary=a.summary,
            source_name=a.source_name, provider=a.provider, published_at=a.published_at, content_hash=chash,
            tickers=a.tickers, is_rumor=rumor, event_id=ev.id,
        ))
        stats["new_articles"] += 1

        for m in extract_tickers(f"{a.title} {a.summary or ''}", a.tickers, names, known_tickers):
            comp = companies.get(m.ticker)
            if comp is None and resolver is not None:
                comp = resolver(m.ticker)  # a stock outside the monitored set: create/link it
                if comp is not None:
                    companies[m.ticker] = comp
                    stats["new_companies"] = stats.get("new_companies", 0) + 1
            if comp is None:
                continue
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
                       materiality=0.6 if u.event_type == "earnings" else 0.5,
                       claim_type="scheduled" if u.status == "scheduled" else "estimated")
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
