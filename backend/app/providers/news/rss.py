"""RSS/Atom ingestion via feedparser.

Only explicitly configured feeds are fetched (no crawling). Each feed host's robots.txt is
checked before fetching, and feeds are fetched with an identifying User-Agent. No attempt is
made to bypass paywalls or anti-bot protections: blocked/failed feeds are skipped and logged.
"""

from __future__ import annotations

import logging
import urllib.robotparser
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import feedparser

from app.providers.news.base import NewsProvider, RawArticle
from app.services.http import HttpClient, ProviderError, RateLimiter, ResponseCache

log = logging.getLogger(__name__)


def parse_feed(text: str, feed_url: str, provider: str = "rss") -> list[RawArticle]:
    parsed = feedparser.parse(text)
    source = (parsed.feed.get("title") if parsed.feed else None) or urlparse(feed_url).netloc
    out: list[RawArticle] = []
    for e in parsed.entries:
        link = e.get("link")
        title = (e.get("title") or "").strip()
        if not link or not title:
            continue
        ts = e.get("published_parsed") or e.get("updated_parsed")
        published = datetime(*ts[:6], tzinfo=UTC) if ts else None
        summary = e.get("summary")
        if summary:
            summary = _strip_html(summary)[:1000]
        out.append(RawArticle(url=link, title=title, summary=summary, source_name=source,
                              published_at=published, provider=provider, kind=_kind_for(feed_url)))
    return out


def _kind_for(url: str) -> str:
    host = urlparse(url).netloc
    if "sec.gov" in host:
        return "press_release"
    if "federalreserve.gov" in host or "bls.gov" in host or "bea.gov" in host:
        return "economic"
    return "news"


def _strip_html(s: str) -> str:
    import re

    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


class RssNewsProvider(NewsProvider):
    name = "rss"

    def __init__(self, feeds: list[str], user_agent: str, cache: ResponseCache | None = None,
                 client: HttpClient | None = None, check_robots: bool = True):
        self.feeds = feeds
        self.user_agent = user_agent
        self.check_robots = check_robots
        self.client = client or HttpClient("rss", headers={"User-Agent": user_agent},
                                           rate_limiter=RateLimiter(2, 1.0), timeout=20, max_retries=2, cache=cache)
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.errors: list[str] = []

    def _allowed(self, url: str) -> bool:
        if not self.check_robots:
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                txt = self.client.get_text(f"{base}/robots.txt", cache_ttl=timedelta(days=1))
                rp.parse(txt.splitlines())
                self._robots[base] = rp
            except ProviderError:
                self._robots[base] = None  # no robots.txt reachable -> allowed per convention
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    def fetch_recent_news(self) -> list[RawArticle]:
        out: list[RawArticle] = []
        for url in self.feeds:
            try:
                if not self._allowed(url):
                    self.errors.append(f"{url}: disallowed by robots.txt; skipped")
                    continue
                text = self.client.get_text(url, cache_ttl=timedelta(minutes=30))
                out.extend(parse_feed(text, url))
            except ProviderError as exc:
                self.errors.append(f"{url}: {exc.message}")
                log.warning("rss feed failed %s: %s", url, exc)
        return out
