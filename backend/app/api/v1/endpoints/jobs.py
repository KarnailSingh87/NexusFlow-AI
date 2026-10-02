"""Background job endpoints.

``POST /jobs`` hands work to the queue and returns immediately with a poll
handle; ``GET /jobs/{id}`` reports progress; ``GET /jobs/{id}/result`` returns
the payload the handler produced. Mounted under ``/api/v1`` to match the rest
of the surface.

The point of this surface is that a 500-page audit is never processed inside a
request. Submitting returns a job id in single-digit milliseconds regardless of
how long the work takes, which is the same contract Nebius Serverless Jobs
offers.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import Field
from sqlalchemy import func, select

from app.api.deps import ConfigDep, CurrentUser, DbSession
from app.core.logging import get_logger
from app.db.models import BackgroundJob, JobLog, JobState
from app.schemas.jobs import (
    JobAccepted,
    JobListResponse,
    JobLogEntry,
    JobQueueStats,
    JobResult,
    JobStatus,
    JobSubmitRequest,
)
from app.services.jobs.store import count_jobs, create_job, request_cancel

router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = get_logger(__name__)


@router.post(
    "",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a background job",
)
async def submit_job(
    body: JobSubmitRequest, session: DbSession, user: CurrentUser, config: ConfigDep
) -> JobAccepted:
    """Queue a job and return immediately.

    ``202 Accepted`` rather than ``201 Created``: the work has been durably
    recorded but not performed. The caller polls the returned id.
    """
    payload = dict(body.payload)
    if body.document_id is not None:
        payload.setdefault("document_id", str(body.document_id))

    job = await create_job(
        session,
        job_type=body.job_type,
        payload=payload,
        submitted_by_id=user.id,
        document_id=body.document_id,
        priority=body.priority,
        max_attempts=body.effective_attempts(config.job_max_attempts),
    )
    await session.commit()

    logger.info(
        "queued job %s type=%s priority=%s user=%s",
        job.id,
        job.job_type,
        job.priority,
        user.email,
    )
    return JobAccepted.from_row(job)


@router.get("", response_model=JobListResponse, summary="List your jobs")
async def list_jobs(
    session: DbSession,
    user: CurrentUser,
    state: Annotated[
        str | None, Query(pattern="^(queued|processing|completed|failed|cancelled)$")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> JobListResponse:
    """Return the caller's jobs, newest first."""
    states = (JobState(state),) if state else None
    total = await count_jobs(session, owner_id=user.id, states=states)
    statement = (
        select(BackgroundJob)
        .where(BackgroundJob.submitted_by_id == user.id)
        .order_by(BackgroundJob.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if states:
        statement = statement.where(BackgroundJob.state.in_([s.value for s in states]))
    rows = await session.scalars(statement)
    return JobListResponse(items=[JobStatus.from_row(row) for row in rows], total=total)


@router.get("/queue", response_model=JobQueueStats, summary="Queue depth")
async def queue_stats(
    session: DbSession, user: CurrentUser, request: Request, config: ConfigDep
) -> JobQueueStats:
    """Report how much work is waiting and whether a worker is consuming it."""
    counts = {}
    for state in JobState:
        counts[state.value] = (
            await session.scalar(
                select(func.count())
                .select_from(BackgroundJob)
                .where(BackgroundJob.state == state.value)
            )
            or 0
        )
    worker = getattr(request.app.state, "job_worker", None)
    return JobQueueStats(
        queued=counts[JobState.QUEUED.value],
        processing=counts[JobState.PROCESSING.value],
        completed=counts[JobState.COMPLETED.value],
        failed=counts[JobState.FAILED.value],
        cancelled=counts[JobState.CANCELLED.value],
        worker_running=bool(worker and worker.running),
        worker_concurrency=config.job_worker_concurrency if config.jobs_enabled else 0,
    )


@router.get("/{job_id}", response_model=JobStatus, summary="Poll a job's status")
async def get_job_status(job_id: uuid.UUID, session: DbSession, user: CurrentUser) -> JobStatus:
    """Return one job's state, progress, and timing.

    A job belonging to another user is reported as 404, not 403: confirming that
    an id exists is itself a leak.
    """
    job = await _load_owned(session, user.id, job_id)
    return JobStatus.from_row(job)


@router.get(
    "/{job_id}/result",
    response_model=JobResult,
    summary="Fetch a job's result payload",
)
async def get_job_result(
    job_id: uuid.UUID, session: DbSession, user: CurrentUser, response: Response
) -> JobResult:
    """Return the handler's output plus the attempt's cost.

    While a job is still running this is ``409 Conflict``: there is genuinely no
    result yet, and returning an empty 200 would let a caller mistake "not done"
    for "produced nothing".
    """
    job = await _load_owned(session, user.id, job_id)

    if not JobState(job.state).is_terminal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Job is {job.state}; no result yet. Poll GET /jobs/{job_id} until it "
                "reaches a terminal state."
            ),
        )
    if job.state == JobState.FAILED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job failed ({job.error_code or 'error'}): {job.error or 'no detail'}",
        )
    return JobResult.from_row(job)


@router.get(
    "/{job_id}/logs",
    response_model=list[JobLogEntry],
    summary="Fetch a job's telemetry log",
)
async def get_job_logs(
    job_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    after: Annotated[int, Field(ge=0)] = 0,
) -> list[JobLogEntry]:
    """Return telemetry lines in order, newest last.

    ``after`` makes polling cheap: a client that has already read sequence 12
    asks for everything above 12 and gets only what is new, instead of refetching
    the whole log on every tick.
    """
    await _load_owned(session, user.id, job_id)
    rows = await session.scalars(
        select(JobLog)
        .where(JobLog.job_id == job_id, JobLog.sequence > after)
        .order_by(JobLog.sequence.asc())
    )
    return [JobLogEntry.from_row(row) for row in rows]


@router.post(
    "/{job_id}/cancel",
    response_model=JobStatus,
    summary="Cancel a queued or running job",
)
async def cancel_job(job_id: uuid.UUID, session: DbSession, user: CurrentUser) -> JobStatus:
    """Stop a job that is queued or running.

    A queued job is cancelled immediately. A running one is flagged: the worker
    notices at its next progress checkpoint and unwinds, so it can clean up. A
    job that already finished is 409, because pretending to cancel completed work
    would hide a real result from the caller.
    """
    job = await _load_owned(session, user.id, job_id)
    if JobState(job.state).is_terminal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job already {job.state}; nothing to cancel.",
        )

    await request_cancel(session, job)
    await session.commit()
    # Reload to avoid any attribute expiration edge cases under certain pools.
    job = await session.get(BackgroundJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    logger.info("cancel requested for job %s (was %s)", job.id, job.state)
    return JobStatus.from_row(job)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a job")
async def delete_job(job_id: uuid.UUID, session: DbSession, user: CurrentUser) -> None:
    """Delete a finished job and its log.

    Telemetry cascades with the row, so deleting the job cannot leave unattributable
    orphan log lines behind.
    """
    job = await _load_owned(session, user.id, job_id)
    if not JobState(job.state).is_terminal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Job is {job.state}. Cancel it first so the worker can stop "
                "before its rows are removed."
            ),
        )
    await session.delete(job)
    await session.commit()


async def _load_owned(session: DbSession, owner_id: uuid.UUID, job_id: uuid.UUID) -> BackgroundJob:
    """Fetch a job, enforcing ownership. 404 rather than 403 on a miss."""
    job = await session.scalar(
        select(BackgroundJob).where(
            BackgroundJob.id == job_id, BackgroundJob.submitted_by_id == owner_id
        )
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return job


__all__ = ["router"]
