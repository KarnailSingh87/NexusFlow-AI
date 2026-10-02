"""Per-job telemetry: duration, token usage, and success metrics.

Three things want to know how a job went: the polling client, the operator
reading logs, and the aggregate counters on ``/metrics``. This module is the
single place that records all three, so a job's ``duration_ms`` can never
disagree with the sum of its log lines.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.metrics import TokenUsage, metrics
from app.db.models import BackgroundJob, JobLog, JobState

logger = get_logger(__name__)

#: Log levels a job line may carry, mirroring the standard names.
LEVELS: frozenset[str] = frozenset({"debug", "info", "warning", "error"})


def _coerce_level(level: str) -> str:
    normalised = level.strip().lower()
    return normalised if normalised in LEVELS else "info"


@dataclass(slots=True)
class JobTelemetry:
    """Accumulates one job attempt's measurements and flushes them to the log.

    Buffered in memory and written once per attempt rather than per event, so a
    job that emits thousands of progress lines cannot turn every step into a
    database round-trip.
    """

    job_id: uuid.UUID
    attempt: int = 0
    started_at: float = field(default_factory=time.perf_counter)
    lines: list[tuple[str, str, str, dict[str, Any]]] = field(default_factory=list)
    tokens: TokenUsage = field(default_factory=TokenUsage)
    token_models: set[str] = field(default_factory=set)

    def elapsed_ms(self) -> int:
        """Milliseconds since this attempt started."""
        return int((time.perf_counter() - self.started_at) * 1000)

    def event(
        self,
        event: str,
        message: str,
        *,
        level: str = "info",
        **data: Any,
    ) -> None:
        """Buffer one telemetry line.

        ``data`` must be JSON-serialisable: it lands in a JSON column and is
        returned verbatim by the log endpoint.
        """
        self.lines.append((_coerce_level(level), event[:64], message[:4000], data))

    def record_tokens(self, usage: TokenUsage, *, model: str | None = None) -> TokenUsage:
        """Add provider-reported token usage for this job."""
        if usage.total_tokens == 0:
            return self.tokens
        self.tokens = self.tokens + usage
        if model:
            self.token_models.add(model)
        return self.tokens

    def summary(self, *, state: JobState, duration_ms: int) -> dict[str, Any]:
        """Build the measurement payload stored on the job row and returned as its result."""
        return {
            "state": state.value,
            "attempt": self.attempt,
            "duration_ms": duration_ms,
            "prompt_tokens": self.tokens.prompt_tokens,
            "completion_tokens": self.tokens.completion_tokens,
            "total_tokens": self.tokens.total_tokens,
            "cached_tokens": self.tokens.cached_tokens,
            "token_models": sorted(self.token_models),
            "log_events": len(self.lines),
        }


async def flush(
    session: AsyncSession,
    telemetry: JobTelemetry,
    *,
    job_type: str,
    state: JobState,
    max_entries: int,
    trim: bool = True,
) -> dict[str, Any]:
    """Persist the buffered lines, return the summary, and update global metrics.

    Every attempt gets a terminal line carrying the duration and token totals,
    so a client that reads only the log still sees the cost of the job.
    """
    duration_ms = telemetry.elapsed_ms()
    telemetry.event(
        f"job.{state.value}",
        f"Job {state.value} after {duration_ms} ms",
        level="info" if state is JobState.COMPLETED else "warning",
        duration_ms=duration_ms,
        total_tokens=telemetry.tokens.total_tokens,
    )

    summary = telemetry.summary(state=state, duration_ms=duration_ms)
    base = await session.scalar(
        select(func.max(JobLog.sequence)).where(JobLog.job_id == telemetry.job_id)
    )
    sequence = 0 if base is None else base + 1
    for level, event, message, data in telemetry.lines:
        session.add(
            JobLog(
                job_id=telemetry.job_id,
                sequence=sequence,
                level=level,
                event=event,
                message=message,
                data=data,
                elapsed_ms=duration_ms,
            )
        )
        sequence += 1
    await session.flush()

    if trim:
        from app.services.jobs.store import trim_logs

        await trim_logs(session, telemetry.job_id, keep=max_entries)

    metrics.record_job(
        state=state,
        job_type=job_type,
        duration_ms=duration_ms,
        usage=telemetry.tokens,
    )
    logger.info(
        "job %s state=%s attempt=%s duration_ms=%s total_tokens=%s events=%s",
        telemetry.job_id,
        state.value,
        telemetry.attempt,
        duration_ms,
        telemetry.tokens.total_tokens,
        len(telemetry.lines),
    )
    return summary


async def record_cancelled(
    session: AsyncSession, job: BackgroundJob, *, reason: str = "cancelled by request"
) -> None:
    """Record the cancellation of a job that never started executing."""
    elapsed = 0
    session.add(
        JobLog(
            job_id=job.id,
            sequence=await _next_sequence(session, job.id),
            level="warning",
            event="job.cancelled",
            message=reason,
            data={"state": JobState.CANCELLED.value},
            elapsed_ms=elapsed,
        )
    )
    metrics.record_job(
        state=JobState.CANCELLED, job_type=job.job_type, duration_ms=elapsed, usage=TokenUsage()
    )
    await session.flush()


async def _next_sequence(session: AsyncSession, job_id: uuid.UUID) -> int:
    current = await session.scalar(select(func.max(JobLog.sequence)).where(JobLog.job_id == job_id))
    return 0 if current is None else current + 1


__all__ = ["JobTelemetry", "flush", "record_cancelled"]
