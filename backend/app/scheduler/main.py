"""Local scheduler service: `python -m app.scheduler.main`.

Runs as its own process (not inside the reloading API server), and only *enqueues* jobs;
the worker executes them. Duplicate protection is layered:
  1. A PostgreSQL advisory lock elects a single active scheduler (others stand by).
  2. Each scheduled run uses a slot dedupe key (e.g. "discovery:live:2026-10-12"), so even
     two schedulers could never enqueue the same slot twice.
Times are America/New_York; price/discovery jobs skip exchange holidays.

On AWS this module is replaced by EventBridge Scheduler rules that start worker tasks.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger as _CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine, session_scope
from app.services import jobs
from app.services.market_calendar import is_trading_day
from app.services.providers import get_data_mode

log = logging.getLogger("eventalpha.scheduler")
NY = ZoneInfo("America/New_York")
LOCK_ID = 727_001  # arbitrary constant for pg_advisory_lock


def slot_key(job_type: str, mode: str, now: datetime, granularity: str) -> str:
    fmt = {"hour": "%Y-%m-%dT%H", "day": "%Y-%m-%d", "week": "%G-W%V", "2hour": "%Y-%m-%dT"}[granularity]
    stamp = now.strftime(fmt) + (f"{now.hour // 2 * 2:02d}" if granularity == "2hour" else "")
    return f"{job_type}:{mode}:{stamp}"


def enqueue_slot(job_type: str, granularity: str, trading_days_only: bool = False,
                 now_fn: Callable[[], datetime] = lambda: datetime.now(NY)) -> str | None:
    now = now_fn()
    if trading_days_only and not is_trading_day(now.date()):
        log.info("skip %s: %s is not a trading day", job_type, now.date())
        return None
    with session_scope() as s:
        mode = get_data_mode(s)
        key = slot_key(job_type, mode, now, granularity)
        job, created = jobs.enqueue(s, job_type, mode, {}, dedupe_key=key, keep_dedupe_key=True)
        log.info("%s %s (%s)", "enqueued" if created else "already queued", job_type, key)
        return key


def CronTrigger(**kw) -> _CronTrigger:  # noqa: N802
    """Cron trigger in New York time. APScheduler 3 does NOT inherit the scheduler's timezone for a trigger
    object built in advance — it uses the container's local zone (UTC), which ran the "after close"
    jobs at 12:45 ET."""
    return _CronTrigger(timezone=NY, **kw)


def build_scheduler() -> BackgroundScheduler:
    s = get_settings()
    sched = BackgroundScheduler(timezone=NY, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 900})
    sched.add_job(enqueue_slot, IntervalTrigger(minutes=s.news_ingest_interval_minutes), args=["ingest_news", "hour"],
                  id="ingest_news", next_run_time=datetime.now(NY))
    sched.add_job(enqueue_slot, CronTrigger(day_of_week="mon-fri", hour=16, minute=45),
                  args=["refresh_prices", "day", True], id="refresh_prices")
    # Overnight catch-up: free market-data plans publish a session's end-of-day data some hours after the
    # close, so the after-close refresh can only see the previous session. These pick up the latest close.
    for hour in (0, 6):
        sched.add_job(enqueue_slot, CronTrigger(day_of_week="tue-sat", hour=hour, minute=30),
                      args=["refresh_prices", "hour"], id=f"refresh_prices_catchup_{hour}")
        sched.add_job(enqueue_slot, CronTrigger(day_of_week="tue-sat", hour=hour, minute=40),
                      args=["refresh_baseline", "hour"], id=f"refresh_baseline_catchup_{hour}")
    sched.add_job(enqueue_slot, CronTrigger(day_of_week="mon-fri", hour="7-20/2", minute=10),
                  args=["sec_monitor", "2hour"], id="sec_monitor")
    sched.add_job(enqueue_slot, CronTrigger(day_of_week="mon-fri", hour=17, minute=5),
                  args=["refresh_baseline", "day", True], id="refresh_baseline")
    sched.add_job(enqueue_slot, CronTrigger(day_of_week="mon-fri", hour=17, minute=30),
                  args=["catalyst_analysis", "day", True], id="catalyst_analysis")
    sched.add_job(enqueue_slot, CronTrigger(day_of_week="mon-fri", hour=18, minute=0),
                  args=["discovery", "day", True], id="discovery")
    sched.add_job(enqueue_slot, CronTrigger(day_of_week="sat", hour=9, minute=0),
                  args=["forecast_review", "week"], id="forecast_review")
    sched.add_job(enqueue_slot, CronTrigger(hour=3, minute=15), args=["cache_cleanup", "day"], id="cache_cleanup")
    sched.add_job(enqueue_slot, CronTrigger(day_of_week="sun", hour=6, minute=0),
                  args=["refresh_universe", "week"], id="refresh_universe")
    return sched


def main() -> None:
    s = get_settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not s.scheduler_enabled:
        log.info("SCHEDULER_ENABLED=false; exiting")
        return
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    conn = get_engine().connect()  # held for the process lifetime: the lock lives with this session
    while not stop.is_set():
        got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_ID}).scalar()
        conn.commit()
        if got:
            break
        log.info("another scheduler holds the leader lock; standing by")
        stop.wait(30)
    if stop.is_set():
        return
    sched = build_scheduler()
    sched.start()
    log.info("scheduler started (leader); jobs: %s", [j.id for j in sched.get_jobs()])
    stop.wait()
    sched.shutdown(wait=False)
    conn.close()


if __name__ == "__main__":
    main()
