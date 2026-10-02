"""Request and response contracts for the background job API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Job kinds a client may submit. Mirrors ``app.db.models.JobType`` but is a
#: literal so an unknown type is a 422 at the edge rather than a job that fails
#: later with "no handler registered".
JobKind = Literal[
    "document_extraction",
    "document_embedding",
    "embedding",
    "workflow_run",
    "custom",
]


class JobSubmitRequest(BaseModel):
    """Body for ``POST /api/v1/jobs``."""

    model_config = ConfigDict(extra="forbid")

    job_type: JobKind = Field(description="Which workload to run.")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Job inputs. ``document_extraction``/``embedding`` need "
            "``document_id``; ``custom``/``workflow_run`` need ``prompt``."
        ),
    )
    document_id: uuid.UUID | None = Field(
        default=None, description="Attach the job to a document for cascade cleanup."
    )
    priority: int = Field(
        default=0, ge=-10, le=10, description="Higher runs first; ties go to the oldest job."
    )
    max_attempts: int | None = Field(
        default=None, ge=1, le=10, description="Overrides JOB_MAX_ATTEMPTS when supplied."
    )

    @model_validator(mode="after")
    def _check_required_inputs(self) -> JobSubmitRequest:
        """Reject an unusable job at the edge rather than mid-worker.

        The handler would reject these too, but catching them here costs no queue
        slot and no retry budget, and returns a 422 instead of an opaque failure
        the caller has to poll for.
        """
        if self.job_type in ("custom", "workflow_run"):
            if not str(self.payload.get("prompt") or "").strip():
                raise ValueError("payload.prompt is required for job_type 'custom'/'workflow_run'.")
        else:
            if self.document_id is None and not self.payload.get("document_id"):
                raise ValueError(
                    "document_id is required for document extraction and embedding jobs."
                )
        return self

    def effective_attempts(self, default: int) -> int:
        """Resolve the retry budget, falling back to the deployment default."""
        return self.max_attempts if self.max_attempts is not None else default


class JobAccepted(BaseModel):
    """Response to a successful submit — the poll handle."""

    id: uuid.UUID
    job_type: str
    state: str
    priority: int
    max_attempts: int
    available_at: datetime
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, job: Any) -> JobAccepted:
        """Build a response from a ``BackgroundJob`` row."""
        return cls(
            id=job.id,
            job_type=job.job_type,
            state=job.state,
            priority=job.priority,
            max_attempts=job.max_attempts,
            available_at=job.available_at,
            created_at=job.created_at,
        )


class JobStatus(BaseModel):
    """Full status view for ``GET /api/v1/jobs/{id}``."""

    id: uuid.UUID
    job_type: str
    state: str
    progress_pct: int = Field(ge=0, le=100)
    priority: int
    attempts: int
    max_attempts: int
    document_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    nebius_job_id: str | None = None
    worker_id: str | None = None
    error: str | None = None
    error_code: str | None = None
    cancel_requested: bool = False
    duration_ms: int | None = None
    created_at: datetime
    updated_at: datetime
    available_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        """Whether polling this job can stop."""
        return self.state in ("completed", "failed", "cancelled")

    @classmethod
    def from_row(cls, job: Any) -> JobStatus:
        """Build a status view from a ``BackgroundJob`` row."""
        return cls(
            id=job.id,
            job_type=job.job_type,
            state=job.state,
            progress_pct=job.progress_pct,
            priority=job.priority,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            document_id=job.document_id,
            run_id=job.run_id,
            nebius_job_id=job.nebius_job_id,
            worker_id=job.worker_id,
            error=job.error,
            error_code=job.error_code,
            cancel_requested=job.cancel_requested,
            duration_ms=job.duration_ms,
            created_at=job.created_at,
            updated_at=job.updated_at,
            available_at=job.available_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            heartbeat_at=job.heartbeat_at,
        )


class JobResult(BaseModel):
    """Result payload for ``GET /api/v1/jobs/{id}/result``."""

    id: uuid.UUID
    job_type: str
    state: str
    duration_ms: int | None = None
    total_tokens: int | None = Field(
        default=None, description="Provider-reported tokens for this attempt, when known."
    )
    token_models: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_row(cls, job: Any) -> JobResult:
        """Split the stored result blob into summary fields and handler output."""
        blob: dict[str, Any] = dict(job.result or {})
        summary = {
            key: blob[key] for key in ("duration_ms", "total_tokens", "token_models") if key in blob
        }
        data = {k: v for k, v in blob.items() if k not in _SUMMARY_KEYS}
        return cls(
            id=job.id,
            job_type=job.job_type,
            state=job.state,
            duration_ms=job.duration_ms
            if job.duration_ms is not None
            else summary.get("duration_ms"),
            total_tokens=summary.get("total_tokens"),
            token_models=list(summary.get("token_models") or []),
            data=data,
        )


#: Keys the worker adds for telemetry; everything else is handler output.
_SUMMARY_KEYS = frozenset(
    {
        "state",
        "attempt",
        "duration_ms",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "token_models",
        "log_events",
    }
)


class JobLogEntry(BaseModel):
    """One telemetry line from ``GET /api/v1/jobs/{id}/logs``."""

    sequence: int
    level: str
    event: str
    message: str
    elapsed_ms: int
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> JobLogEntry:
        """Build an entry from a ``JobLog`` row."""
        return cls(
            sequence=row.sequence,
            level=row.level,
            event=row.event,
            message=row.message,
            elapsed_ms=row.elapsed_ms,
            data=dict(row.data or {}),
            created_at=row.created_at,
        )


class JobListResponse(BaseModel):
    """Paginated list of the caller's jobs, newest first."""

    items: list[JobStatus]
    total: int


class JobQueueStats(BaseModel):
    """Queue depth for ``GET /api/v1/jobs/queue``."""

    queued: int
    processing: int
    completed: int
    failed: int
    cancelled: int
    worker_running: bool
    worker_concurrency: int


__all__ = [
    "JobAccepted",
    "JobKind",
    "JobListResponse",
    "JobLogEntry",
    "JobQueueStats",
    "JobResult",
    "JobStatus",
    "JobSubmitRequest",
]
