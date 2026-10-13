"""PostgreSQL-backed job queue (SELECT ... FOR UPDATE SKIP LOCKED).

API requests enqueue jobs and return immediately; the worker process executes them.
`dedupe_key` prevents duplicate jobs: for on-demand jobs it is released when the job
finishes; for scheduled slots (e.g. "news:2026-10-12T14") it is kept so a slot can only
ever run once even if several schedulers start.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AnalysisJob

ACTIVE = ("queued", "running")


def enqueue(session: Session, job_type: str, data_mode: str, payload: dict[str, Any] | None = None,
            dedupe_key: str | None = None, keep_dedupe_key: bool = False) -> tuple[AnalysisJob, bool]:
    """Returns (job, created). If a job with the same dedupe key exists, returns it."""
    if dedupe_key:
        existing = session.scalar(select(AnalysisJob).where(AnalysisJob.dedupe_key == dedupe_key))
        if existing:
            return existing, False
    job = AnalysisJob(id=uuid.uuid4(), job_type=job_type, data_mode=data_mode,
                      payload={**(payload or {}), "_keep_dedupe": keep_dedupe_key}, dedupe_key=dedupe_key)
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        existing = session.scalar(select(AnalysisJob).where(AnalysisJob.dedupe_key == dedupe_key))
        assert existing is not None
        return existing, False
    return job, True


def claim_next(session: Session, worker_id: str) -> AnalysisJob | None:
    job = session.scalar(
        select(AnalysisJob).where(AnalysisJob.status == "queued").order_by(AnalysisJob.created_at)
        .with_for_update(skip_locked=True).limit(1)
    )
    if job is None:
        return None
    job.status = "running"
    job.started_at = datetime.now(UTC)
    job.heartbeat_at = job.started_at
    job.locked_by = worker_id
    job.attempts += 1
    session.flush()
    return job


def finish(session: Session, job_id: uuid.UUID, ok: bool, result: Any = None, error: str | None = None) -> None:
    job = session.get(AnalysisJob, job_id)
    if job is None:
        return
    job.status = "succeeded" if ok else "failed"
    job.result = result
    job.error = error
    job.finished_at = datetime.now(UTC)
    if not (job.payload or {}).get("_keep_dedupe"):
        job.dedupe_key = None


def set_progress(session: Session, job_id: uuid.UUID, msg: str) -> None:
    session.execute(update(AnalysisJob).where(AnalysisJob.id == job_id).values(progress=msg[:250]))


HEARTBEAT_INTERVAL = timedelta(seconds=30)
STALE_AFTER = timedelta(minutes=3)


def heartbeat(session: Session, job_id: uuid.UUID) -> None:
    session.execute(update(AnalysisJob).where(AnalysisJob.id == job_id, AnalysisJob.status == "running")
                    .values(heartbeat_at=datetime.now(UTC)))


def recover_stale(session: Session, older_than: timedelta = STALE_AFTER) -> int:
    """Fail 'running' jobs whose worker stopped heartbeating (crash, restart, container replaced).

    They are failed, not silently retried, and their dedupe key is released so the job can be
    started again. Discovery runs left 'running' by such a job are marked failed too."""
    from sqlalchemy import func

    from app.models import DiscoveryRun

    now = datetime.now(UTC)
    last_seen = func.coalesce(AnalysisJob.heartbeat_at, AnalysisJob.started_at)
    stale = list(session.scalars(select(AnalysisJob).where(AnalysisJob.status == "running",
                                                           last_seen < now - older_than)))
    for j in stale:
        j.status, j.finished_at = "failed", now
        j.error = f"worker {j.locked_by or ''} stopped before completion (no heartbeat); safe to re-run".strip()
        if not (j.payload or {}).get("_keep_dedupe"):
            j.dedupe_key = None
        if j.job_type == "discovery":
            for run in session.scalars(select(DiscoveryRun).where(DiscoveryRun.status == "running",
                                                                  DiscoveryRun.data_mode == j.data_mode,
                                                                  DiscoveryRun.started_at <= now)):
                run.status, run.finished_at = "failed", now
    return len(stale)
