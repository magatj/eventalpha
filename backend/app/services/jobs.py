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


def recover_stale(session: Session, older_than: timedelta = timedelta(minutes=45)) -> int:
    """Jobs left 'running' by a crashed worker are failed (not silently retried)."""
    cutoff = datetime.now(UTC) - older_than
    stale = list(session.scalars(select(AnalysisJob).where(AnalysisJob.status == "running",
                                                           AnalysisJob.started_at < cutoff)))
    for j in stale:
        j.status, j.error, j.finished_at = "failed", "worker stopped before completion", datetime.now(UTC)
        if not (j.payload or {}).get("_keep_dedupe"):
            j.dedupe_key = None
    return len(stale)
