"""Background worker: `python -m app.worker.main`.

Polls the analysis_jobs table and runs jobs one at a time. Safe to run several workers:
jobs are claimed with FOR UPDATE SKIP LOCKED.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import traceback

from app.config import get_settings
from app.db import session_scope
from app.services import jobs
from app.services.analysis import AnalysisError
from app.services.http import ProviderError
from app.worker.handlers import HANDLERS

log = logging.getLogger("eventalpha.worker")


def run_one(worker_id: str) -> bool:
    """Claim and run one job. Returns False when the queue is empty."""
    with session_scope() as s:
        job = jobs.claim_next(s, worker_id)
        if job is None:
            return False
        job_id, job_type = job.id, job.job_type
    log.info("job %s (%s) started", job_id, job_type)

    def progress(msg: str) -> None:
        with session_scope() as ps:
            jobs.set_progress(ps, job_id, msg)

    try:
        with session_scope() as s:
            job = s.get(jobs.AnalysisJob, job_id)
            handler = HANDLERS.get(job_type)
            if handler is None:
                raise ValueError(f"unknown job type {job_type}")
            result = handler(s, job, progress)
        with session_scope() as s:
            jobs.finish(s, job_id, True, result=result)
        log.info("job %s succeeded", job_id)
    except (AnalysisError, ProviderError) as exc:
        with session_scope() as s:
            jobs.finish(s, job_id, False, error=str(exc))
        log.warning("job %s failed: %s", job_id, exc)
    except Exception as exc:
        with session_scope() as s:
            jobs.finish(s, job_id, False, error=f"{type(exc).__name__}: {exc}")
        log.error("job %s crashed:\n%s", job_id, traceback.format_exc())
    return True


def loop(stop: threading.Event, worker_id: str | None = None) -> None:
    worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    poll = get_settings().worker_poll_seconds
    with session_scope() as s:
        n = jobs.recover_stale(s)
        if n:
            log.warning("marked %d stale running job(s) as failed", n)
    while not stop.is_set():
        try:
            if not run_one(worker_id):
                stop.wait(poll)
        except Exception:
            log.exception("worker loop error; backing off")
            stop.wait(5)


def main() -> None:
    logging.basicConfig(level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("worker started")
    loop(stop)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
