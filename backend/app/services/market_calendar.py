"""NYSE trading calendar (holidays + early closes) via exchange_calendars."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")


@lru_cache(maxsize=1)
def _calendar():
    import exchange_calendars as xcals

    return xcals.get_calendar("XNYS", start="2015-01-01")


def to_ny_date(ts_ms: int) -> date:
    """Convert a vendor epoch-millisecond bar timestamp to the US/Eastern trading date."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).astimezone(NY).date()


def is_trading_day(d: date) -> bool:
    cal = _calendar()
    ts = pd.Timestamp(d)
    if ts > cal.last_session:
        return ts.weekday() < 5
    return bool(cal.is_session(ts))


def session_close(d: date) -> datetime | None:
    """Close time (UTC) for a session, honoring early closes. None if not a session."""
    cal = _calendar()
    ts = pd.Timestamp(d)
    if not cal.is_session(ts):
        return None
    return cal.session_close(ts).to_pydatetime()


def is_early_close(d: date) -> bool:
    close = session_close(d)
    return bool(close and close.astimezone(NY).time() < time(16, 0))


def last_completed_session(now: datetime | None = None) -> date:
    """Most recent session whose close (incl. a 30-minute settle buffer) has passed."""
    now = now or datetime.now(UTC)
    cal = _calendar()
    d = now.astimezone(NY).date()
    for _ in range(15):
        ts = pd.Timestamp(d)
        if cal.is_session(ts):
            close = cal.session_close(ts).to_pydatetime()
            if now >= close + timedelta(minutes=30):
                return d
        d -= timedelta(days=1)
    raise RuntimeError("could not determine last completed session")


def previous_session(d: date) -> date:
    """The last session strictly before `d`."""
    cal = _calendar()
    return cal.date_to_session(pd.Timestamp(d - timedelta(days=1)), direction="previous").date()


def sessions_between(start: date, end: date) -> list[date]:
    cal = _calendar()
    return [s.date() for s in cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))]


def first_session_on_or_after(d: date) -> date:
    cal = _calendar()
    return cal.date_to_session(pd.Timestamp(d), direction="next").date()


def trading_days_stale(price_date: date, now: datetime | None = None) -> int:
    """Number of completed sessions after `price_date` (0 = up to date)."""
    last = last_completed_session(now)
    if price_date >= last:
        return 0
    return max(0, len(sessions_between(price_date, last)) - 1)
