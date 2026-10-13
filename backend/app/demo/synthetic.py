"""Deterministic SYNTHETIC demo data. Nothing here is real market or company data.

Every company name is suffixed with "(DEMO)", every URL points to demo.eventalpha.invalid,
and every API response in demo mode carries data_mode="demo" so the UI labels it.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, date, datetime, time, timedelta

import numpy as np

from app.providers.market.base import (
    CompanyProfile,
    CorporateActionData,
    IndexQuote,
    LatestPrice,
    MarketDataProvider,
    PriceBar,
    ProviderArticle,
)
from app.providers.sec.xbrl import FinancialFact
from app.services.http import DataUnavailable
from app.services.market_calendar import last_completed_session, sessions_between

DEMO_SOURCE = "demo_synthetic"
DEMO_URL = "https://demo.eventalpha.invalid"

DEMO_UNIVERSE: dict[str, tuple[str, str, str]] = {
    # ticker: (sector, sic, industry)
    "NVDA": ("Technology", "3674", "Semiconductors"),
    "MSFT": ("Technology", "7372", "Software"),
    "AAPL": ("Technology", "3571", "Computer hardware"),
    "AVGO": ("Technology", "3674", "Semiconductors"),
    "TSM": ("Technology", "3674", "Semiconductor foundry"),
    "VRT": ("Industrials", "3585", "Electrical equipment"),
    "NKE": ("Consumer Discretionary", "3021", "Footwear"),
    "AMZN": ("Consumer Discretionary", "5961", "Online retail"),
    "GOOGL": ("Communication Services", "7370", "Internet services"),
    "META": ("Communication Services", "7370", "Internet services"),
    "JPM": ("Financials", "6021", "Banking"),
    "XOM": ("Energy", "2911", "Integrated oil"),
    "LLY": ("Health Care", "2834", "Pharmaceuticals"),
    "UNH": ("Health Care", "6324", "Managed care"),
    "COST": ("Consumer Staples", "5331", "Warehouse retail"),
    "CAT": ("Industrials", "3531", "Machinery"),
    "AMD": ("Technology", "3674", "Semiconductors"),
    "CRM": ("Technology", "7372", "Software"),
    "TSLA": ("Consumer Discretionary", "3711", "Automobiles"),
    "NEE": ("Utilities", "4911", "Electric utilities"),
    "PLTR": ("Technology", "7372", "Software"),
    "ORCL": ("Technology", "7372", "Software"),
}


def _seed(ticker: str, salt: str = "") -> int:
    return int(hashlib.sha256(f"{ticker}|{salt}".encode()).hexdigest()[:8], 16)


def _rng(ticker: str, salt: str = "") -> np.random.Generator:
    return np.random.default_rng(_seed(ticker, salt))


def demo_params(ticker: str) -> dict:
    r = _rng(ticker, "params")
    return {
        "start_price": float(r.uniform(20, 400)),
        "drift": float(r.uniform(-0.05, 0.30)),
        "vol": float(r.uniform(0.18, 0.50)),
        "revenue_q0": float(r.uniform(1.5e9, 30e9)),
        "rev_growth": float(r.uniform(-0.03, 0.35)),
        # ~15% of demo companies are loss-making; the rest are clearly profitable.
        "net_margin": float(r.uniform(-0.08, -0.01)) if r.uniform() < 0.15 else float(r.uniform(0.04, 0.30)),
        "fcf_margin_delta": float(r.uniform(-0.04, 0.06)),
        "shares": float(r.uniform(3e8, 5e9)),
        "share_rate": float(r.uniform(-0.03, 0.02)),
        "debt_ratio": float(r.uniform(0.0, 1.5)),
    }


def is_valid_demo_ticker(ticker: str) -> bool:
    return ticker.isalpha() and 1 <= len(ticker) <= 5


class DemoMarketProvider(MarketDataProvider):
    name = DEMO_SOURCE
    is_demo = True
    coverage_notes = ["DEMO MODE: all prices are synthetic random walks seeded by ticker. Not real data."]

    def __init__(self, as_of: date | None = None):
        self.as_of = as_of or last_completed_session()

    def _series(self, ticker: str) -> list[PriceBar]:
        p = demo_params(ticker)
        days = sessions_between(self.as_of - timedelta(days=3 * 365), self.as_of)
        r = _rng(ticker, "prices")
        dt = 1 / 252
        shocks = r.normal((p["drift"] - 0.5 * p["vol"] ** 2) * dt, p["vol"] * math.sqrt(dt), len(days))
        # Anchor the synthetic price to the synthetic fundamentals *today*: the walk is rescaled so
        # the latest close sits at a trailing P/E of 16–32x (or 1–4x sales for loss-makers).
        closes = np.exp(np.cumsum(shocks))
        ttm_rev = p["revenue_q0"] * 4 * (1 + p["rev_growth"]) ** 2.75
        shares_now = p["shares"] * (1 + p["share_rate"] / 4) ** 12
        eps = ttm_rev * p["net_margin"] / shares_now
        if p["net_margin"] >= 0.02:
            target = eps * float(r.uniform(16, 32))
        else:  # loss-makers trade on sales
            target = ttm_rev / shares_now * float(r.uniform(2, 6))
        closes = closes * (max(target, 2.0) / closes[-1])
        bars = []
        for d, c, z in zip(days, closes, r.normal(0, 1, len(days)), strict=False):
            spread = abs(z) * p["vol"] * math.sqrt(dt) * c
            bars.append(PriceBar(date=d, open=float(c * (1 + 0.002 * z)), high=float(c + spread),
                                 low=float(max(0.01, c - spread)), close=float(c),
                                 volume=float(abs(r.normal(8e6, 2e6)) + 5e5), vwap=float(c)))
        return bars

    def get_latest_price(self, ticker: str) -> LatestPrice:
        b = self._series(ticker)[-1]
        return LatestPrice(ticker=ticker, price=b.close, as_of_date=b.date, kind="previous_close",
                           source=DEMO_SOURCE, volume=b.volume,
                           as_of_timestamp=datetime.combine(b.date, time(20, 0), tzinfo=UTC))

    def get_historical_prices(self, ticker: str, start_date: date, end_date: date) -> list[PriceBar]:
        return [b for b in self._series(ticker) if start_date <= b.date <= end_date]

    def get_company_profile(self, ticker: str) -> CompanyProfile:
        if not is_valid_demo_ticker(ticker):
            raise DataUnavailable(DEMO_SOURCE, f"{ticker} is not a valid ticker format")
        sector, sic, industry = DEMO_UNIVERSE.get(ticker, ("Industrials", "3560", "General industrial"))
        p = demo_params(ticker)
        last = self._series(ticker)[-1].close
        return CompanyProfile(
            ticker=ticker, name=f"{ticker} Synthetic Corp (DEMO)", exchange="DEMO", cik=None, sic_code=sic,
            sic_description=f"{industry} (demo)", market_cap=last * p["shares"], shares_outstanding=p["shares"],
            currency="USD", source=DEMO_SOURCE,
        )

    def get_corporate_actions(self, ticker: str) -> list[CorporateActionData]:
        return []

    def get_market_indices(self) -> list[IndexQuote]:
        out = []
        for sym, name in (("DEMO-SPX", "S&P 500"), ("DEMO-NDX", "Nasdaq-100"), ("DEMO-DJI", "Dow Jones Industrial Average")):
            bars = self._series(sym.replace("-", ""))
            scale = {"DEMO-SPX": 6000, "DEMO-NDX": 21000, "DEMO-DJI": 44000}[sym] / bars[-1].close
            out.append(IndexQuote(symbol=sym, name=f"{name} (synthetic demo)", value=bars[-1].close * scale,
                                  change_pct=bars[-1].close / bars[-2].close - 1, as_of_date=bars[-1].date,
                                  kind="index", proxy_for=name, source=DEMO_SOURCE))
        return out

    def get_company_news(self, ticker: str, limit: int = 20) -> list[ProviderArticle]:
        return demo_articles(self.as_of, [ticker])[:limit]

    def get_market_news(self, limit: int = 1000) -> list[ProviderArticle]:
        # Deterministic subset: about half the demo universe has market-wide coverage.
        covered = [t for t in DEMO_UNIVERSE if _seed(t, "mkt-news") % 2 == 0]
        return demo_articles(self.as_of, covered)[:limit]

    def benchmark_series(self) -> list[PriceBar]:
        return self._series("SPY")


def demo_financial_facts(ticker: str, as_of: date) -> list[FinancialFact]:
    """12 quarters + 3 fiscal years of synthetic, internally consistent statements."""
    p = demo_params(ticker)
    r = _rng(ticker, "fin")
    facts: list[FinancialFact] = []
    # quarter ends: calendar quarters, the last one ending >= 45 days before as_of
    last_end = date(as_of.year, ((as_of.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)
    if (as_of - last_end).days < 45:
        last_end = date(last_end.year, ((last_end.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)
    ends = []
    e = last_end
    for _ in range(12):
        ends.append(e)
        e = date(e.year, ((e.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)
    ends.reverse()
    qg = (1 + p["rev_growth"]) ** 0.25 - 1
    rev = p["revenue_q0"]
    shares = p["shares"]
    annual: dict[int, dict[str, float]] = {}
    for end in ends:
        start = date(end.year, end.month - 2, 1)
        rev *= 1 + qg + r.normal(0, 0.015)
        margin = p["net_margin"] + r.normal(0, 0.01)
        ni = rev * margin
        oi = rev * (margin + 0.06)
        ocf = ni * 1.15 + rev * 0.02
        capex = rev * max(0.02, 0.06 - p["fcf_margin_delta"])
        shares *= 1 + p["share_rate"] / 4
        fy = end.year
        fp = f"Q{(end.month - 1) // 3 + 1}"
        accn = f"DEMO-{ticker}-{end:%Y%m}"
        filed = end + timedelta(days=35)
        def add(metric: str, val: float, unit: str = "USD", ptype: str = "quarterly", s: date | None = start,
                fp_: str | None = fp, e_: date = end, a: str = accn, f: date = filed, fy_: int = fy) -> None:
            facts.append(FinancialFact(metric=metric, period_type=ptype, period_start=s, period_end=e_, value=val,
                                       unit=unit, concept=f"Demo{metric.title().replace('_', '')}", taxonomy="demo",
                                       accession=a, form="DEMO", filed_date=f, fiscal_year=fy_, fiscal_period=fp_))
        add("revenue", rev)
        add("net_income", ni)
        add("operating_income", oi)
        add("gross_profit", rev * (margin + 0.25))
        add("operating_cash_flow", ocf)
        add("capex", capex)
        add("diluted_shares", shares, "shares")
        add("eps_diluted", ni / shares, "USD/shares")
        add("cash", rev * 0.8, ptype="instant", s=None)
        add("long_term_debt", rev * p["debt_ratio"], ptype="instant", s=None)
        add("stockholders_equity", rev * 2.5, ptype="instant", s=None)
        add("total_assets", rev * 5, ptype="instant", s=None)
        add("shares_outstanding", shares, "shares", ptype="instant", s=None)
        a = annual.setdefault(fy, {"revenue": 0, "net_income": 0, "operating_income": 0, "gross_profit": 0,
                                   "operating_cash_flow": 0, "capex": 0, "n": 0, "end": 0})
        for m, v in (("revenue", rev), ("net_income", ni), ("operating_income", oi), ("gross_profit", rev * (margin + 0.25)),
                     ("operating_cash_flow", ocf), ("capex", capex)):
            a[m] += v
        a["n"] += 1
        a["shares"] = shares
        a["end"] = end  # type: ignore[assignment]
    for fy, a in annual.items():
        if a["n"] != 4:
            continue
        end = a["end"]
        accn = f"DEMO-{ticker}-{fy}FY"
        for m in ("revenue", "net_income", "operating_income", "gross_profit", "operating_cash_flow", "capex"):
            facts.append(FinancialFact(metric=m, period_type="annual", period_start=date(fy, 1, 1), period_end=end,
                                       value=a[m], unit="USD", concept=f"Demo{m}", taxonomy="demo", accession=accn,
                                       form="DEMO", filed_date=end + timedelta(days=55), fiscal_year=fy,
                                       fiscal_period="FY"))
        facts.append(FinancialFact(metric="diluted_shares", period_type="annual", period_start=date(fy, 1, 1),
                                   period_end=end, value=a["shares"], unit="shares", concept="DemoShares",
                                   taxonomy="demo", accession=accn, form="DEMO", filed_date=end + timedelta(days=55),
                                   fiscal_year=fy, fiscal_period="FY"))
    return facts


_DEMO_HEADLINES = [
    ("{t} announces expanded data-center product line", "product_launch", "positive", 2),
    ("{t} schedules quarterly earnings release", "earnings", "uncertain", -10),
    ("Report: {t} reportedly in talks for acquisition, sources say", "m_and_a", "uncertain", 1),
    ("{t} files 8-K: entry into material supply agreement", "sec_filing", "positive", 3),
    ("Regulators open review of {t} pricing practices", "regulatory", "negative", 5),
    ("{t} raises full-year revenue guidance", "guidance", "positive", 6),
    ("{t} recalls units after supplier defect", "legal", "negative", 8),
]


def demo_articles(as_of: date, tickers: list[str]) -> list[ProviderArticle]:
    out: list[ProviderArticle] = []
    for t in tickers:
        r = _rng(t, "news")
        picks = r.choice(len(_DEMO_HEADLINES), size=3, replace=False)
        for idx in picks:
            title, _, _, days_ago = _DEMO_HEADLINES[int(idx)]
            pub = datetime.combine(as_of - timedelta(days=abs(days_ago) % 7), time(13, 30), tzinfo=UTC)
            out.append(ProviderArticle(
                url=f"{DEMO_URL}/news/{t.lower()}/{int(idx)}", title="[DEMO] " + title.format(t=t),
                summary=f"Synthetic demonstration article about {t}. Not real news.", source_name="Demo Newswire",
                published_at=pub, tickers=[t], provider=DEMO_SOURCE,
            ))
    return out


def demo_upcoming_events(as_of: date, tickers: list[str]) -> list[dict]:
    out = []
    for t in tickers:
        r = _rng(t, "cal")
        out.append({
            "title": f"[DEMO] {t} estimated next earnings release",
            "event_type": "earnings", "status": "estimated",
            "event_date": as_of + timedelta(days=int(r.integers(5, 80))), "tickers": [t],
            "url": f"{DEMO_URL}/calendar/{t.lower()}", "source_name": "Demo calendar",
        })
    out.append({"title": "[DEMO] Synthetic central-bank rate decision", "event_type": "economic_release",
                "status": "scheduled", "event_date": as_of + timedelta(days=12), "tickers": [],
                "url": f"{DEMO_URL}/calendar/macro", "source_name": "Demo calendar"})
    return out
