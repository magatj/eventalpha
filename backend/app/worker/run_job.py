"""Run one job type to completion and exit: `python -m app.worker.run_job discovery`.

Used by AWS EventBridge Scheduler → ECS RunTask (replacing the local APScheduler service).
The job is recorded in analysis_jobs exactly like locally scheduled jobs.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime

from app.config import get_settings
from app.db import session_scope
from app.services import jobs
from app.services.market_calendar import NY, is_trading_day
from app.services.providers import get_data_mode
from app.worker.handlers import HANDLERS
from app.worker.main import run_one

TRADING_DAY_JOBS = {"refresh_prices", "catalyst_analysis", "discovery"}


def main(argv: list[str]) -> int:
    logging.basicConfig(level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if len(argv) != 1 or argv[0] not in HANDLERS:
        print(f"usage: python -m app.worker.run_job <{'|'.join(HANDLERS)}>", file=sys.stderr)
        return 2
    job_type = argv[0]
    if job_type in TRADING_DAY_JOBS and not is_trading_day(datetime.now(NY).date()):
        logging.info("%s skipped: not an NYSE trading day", job_type)
        return 0
    with session_scope() as s:
        mode = get_data_mode(s)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
        job, created = jobs.enqueue(s, job_type, mode, {}, dedupe_key=f"{job_type}:{mode}:{stamp}:eventbridge",
                                    keep_dedupe_key=True)
        job_id = job.id
    if not created:
        logging.info("job already enqueued for this slot; exiting")
        return 0
    while run_one("eventbridge-task"):
        with session_scope() as s:
            if s.get(jobs.AnalysisJob, job_id).status in ("succeeded", "failed"):
                break
    with session_scope() as s:
        status = s.get(jobs.AnalysisJob, job_id).status
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
