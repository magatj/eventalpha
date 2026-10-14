"""Official index closes from FRED (Federal Reserve Bank of St. Louis), free and keyless.

FRED republishes the daily closing levels of the S&P 500 (series SP500, from S&P Dow Jones Indices),
the Dow Jones Industrial Average (DJIA) and the Nasdaq-100 (NASDAQ100). Closes usually appear the
evening of the session or the next morning; FRED leaves a blank for a session not yet published, so
the latest *published* close is used and its date is always shown.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from app.services.http import DataUnavailable, HttpClient, ResponseCache

PROVIDER = "fred"
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
SERIES = {"SP500": "S&P 500", "DJIA": "Dow Jones Industrial Average", "NASDAQ100": "Nasdaq-100"}


def parse_fred_csv(text: str) -> dict[str, list[tuple[date, float]]]:
    """{series_id: [(date, value), ...]} ascending; blank / '.' values (not yet published, holidays) skipped."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or len(rows[0]) < 2:
        raise DataUnavailable(PROVIDER, "unexpected FRED response")
    header = [h.strip() for h in rows[0]]
    out: dict[str, list[tuple[date, float]]] = {h: [] for h in header[1:]}
    for r in rows[1:]:
        if not r:
            continue
        try:
            d = date.fromisoformat(r[0].strip())
        except ValueError:
            continue
        for h, v in zip(header[1:], r[1:], strict=False):
            v = v.strip()
            if v and v != ".":
                try:
                    out[h].append((d, float(v)))
                except ValueError:
                    continue
    return out


class FredIndexSource:
    def __init__(self, cache: ResponseCache | None = None, client: HttpClient | None = None):
        self.client = client or HttpClient(PROVIDER, headers={"Accept": "text/csv"}, timeout=20.0, max_retries=2,
                                           cache=cache)

    def latest(self, series: list[str], lookback_days: int = 21) -> dict[str, tuple[date, float, float | None]]:
        """{series: (date, close, previous close)} for the latest published session of each series."""
        start = date.today() - timedelta(days=lookback_days)
        text = self.client.get_text(CSV_URL, {"id": ",".join(series), "cosd": start.isoformat()},
                                    cache_ttl=timedelta(minutes=30))
        data = parse_fred_csv(text)
        out = {}
        for s in series:
            pts = data.get(s) or []
            if pts:
                out[s] = (pts[-1][0], pts[-1][1], pts[-2][1] if len(pts) > 1 else None)
        return out
