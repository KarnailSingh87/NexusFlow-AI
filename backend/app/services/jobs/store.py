"""Persistence helpers for the background job queue.

The queue is *database backed*: the ``background_jobs`` table is both the
hand-off and the audit trail, so a job survives an API restart. Nothing is held
only in memory, which is what lets the submit endpoint return immediately and
the poll endpoint read the truth from the database rather than from a guess.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BackgroundJob, JobLog, JobState

#: Terminal states, in the order a client most often wants to filter them.
TERMINAL_STATES: tuple[JobState, ...] = (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED)


def utcnow() -> datetime:
    """Timezone-aware now, in one place so tests can reason about comparisons."""
    return datetime.now(tz=UTC)


async def create_job(
    session: AsyncSession,
    *,
    job_type: str,
    payload: dict[str, Any],
    submitted_by_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    priority: int = 0,
    max_attempts: int = 3,
    available_at: datetime | None = None,
) -> BackgroundJob:
    """Insert a queued job and return it.

    The row is written *before* any worker can see it, so a job is never lost to
    a crash between accepting the request and scheduling the work.
    """
    job = BackgroundJob(
        job_type=job_type,
        state=JobState.QUEUED.value,
        payload=dict(payload),
        submitted_by_id=submitted_by_id,
        document_id=document_id,
        run_id=run_id,
        priority=priority,
        max_attempts=max(1, max_attempts),
        available_at=available_at or utcnow(),
    )
    session.add(job)
    await session.flush()
    return job


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> BackgroundJob | None:
    """Fetch one job by id, or ``None``."""
    return await session.get(BackgroundJob, job_id)


async def count_jobs(
    session: AsyncSession, *, owner_id: uuid.UUID, states: tuple[JobState, ...] | None = None
) -> int:
    """Count a user's jobs, optionally restricted to ``states``."""
    statement = (
        select(func.count())
        .select_from(BackgroundJob)
        .where(BackgroundJob.submitted_by_id == owner_id)
    )
    if states:
        statement = statement.where(BackgroundJob.state.in_([s.value for s in states]))
    return await session.scalar(statement) or 0


async def request_cancel(session: AsyncSession, job: BackgroundJob) -> bool:
    """Ask a job to stop.

    Returns True when the job was still live. A queued job is cancelled outright;
    a running one only gets the flag, because the worker owns its own unwind and
    killing it from here would strand its database session.
    """
    current = JobState(job.state)
    if current.is_terminal:
        return False

    job.cancel_requested = True
    if current is JobState.QUEUED:
        job.state = JobState.CANCELLED.value
        job.finished_at = utcnow()
    await session.flush()
    return True


async def trim_logs(session: AsyncSession, job_id: uuid.UUID, *, keep: int) -> int:
    """Delete the oldest log lines beyond ``keep`` for one job.

    Telemetry is a debugging aid, not an audit requirement; unbounded growth on
    a chatty job would eventually dominate the database.
    """
    if keep <= 0:
        return 0
    total = await session.scalar(
        select(func.count()).select_from(JobLog).where(JobLog.job_id == job_id)
    )
    surplus = (total or 0) - keep
    if surplus <= 0:
        return 0

    # Find the oldest row that must be kept, then delete everything up to and
    # including it in one statement rather than reading surplus ids into memory.
    # Offset is surplus-1 so the surviving set is exactly the newest `keep` rows.
    cutoff = await session.scalar(
        select(JobLog.sequence)
        .where(JobLog.job_id == job_id)
        .order_by(JobLog.sequence.asc())
        .limit(1)
        .offset(surplus - 1)
    )
    if cutoff is None:
        return 0
    result = await session.execute(
        delete(JobLog).where(JobLog.job_id == job_id, JobLog.sequence <= cutoff)
    )
    # ``execute`` on a DML statement returns a CursorResult at runtime, but the
    # async signature is typed loosely, so narrow it rather than ignoring.
    return int(getattr(result, "rowcount", 0) or 0)


async def reap_stale_jobs(session: AsyncSession, *, older_than_seconds: float) -> list[uuid.UUID]:
    """Return claimed jobs with an expired heartbeat to the queue.

    A worker that is OOM-killed mid-job cannot mark its own job failed, so the
    job would sit in ``processing`` forever. The heartbeat is the liveness proof:
    if it has gone stale, whoever is running the job is gone too.
    """
    cutoff = utcnow() - timedelta(seconds=older_than_seconds)
    rows = await session.scalars(
        select(BackgroundJob).where(
            BackgroundJob.state == JobState.PROCESSING.value,
            BackgroundJob.cancel_requested.is_(False),
            BackgroundJob.heartbeat_at.is_not(None),
            BackgroundJob.heartbeat_at < cutoff,
        )
    )
    reaped: list[uuid.UUID] = []
    for job in rows:
        job.state = JobState.QUEUED.value
        job.worker_id = None
        job.progress_pct = 0
        job.available_at = utcnow()
        job.error = f"Reclaimed: no heartbeat for {older_than_seconds:.0f}s."
        job.error_code = "stale_worker"
        job.started_at = None
        reaped.append(job.id)
    if reaped:
        await session.flush()
    return reaped


__all__ = [
    "TERMINAL_STATES",
    "count_jobs",
    "create_job",
    "get_job",
    "reap_stale_jobs",
    "request_cancel",
    "trim_logs",
    "utcnow",
]
