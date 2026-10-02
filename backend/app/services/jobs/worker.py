"""In-process asyncio worker pool backed by the ``background_jobs`` table.

Design notes
------------
**Why the database is the queue.** An in-memory ``asyncio.Queue`` would lose
every pending job on restart and would be invisible to a second process. Here
the row *is* the queue: a job submitted before the worker starts is picked up
whenever it does, and a job whose worker died is visible for reclaiming.

**Why N polling workers rather than one task per job.** An unbounded
``create_task`` per submission would let a burst of uploads exhaust memory with
CPU-bound extractions all fighting over the GIL. A fixed pool turns that burst
into back-pressure: work waits in the table, where it is durable and observable.

**Claiming is atomic.** ``FOR UPDATE SKIP LOCKED`` (PostgreSQL) lets several
workers -- or several containers -- claim disjoint jobs without contending. The
SQLite path used by tests has no such lock, so claiming falls back to a compare
and swap on ``state``.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.config import settings as global_settings
from app.core.logging import get_logger
from app.db.models import BackgroundJob, JobState
from app.services.jobs import handlers as job_handlers
from app.services.jobs.store import reap_stale_jobs, utcnow
from app.services.jobs.telemetry import JobTelemetry, flush, record_cancelled

logger = get_logger(__name__)

#: Heartbeat cadence while a job runs. Frequent enough that ``job_stale_after``
#: only fires on a genuinely dead worker, rare enough not to hammer the database.
_HEARTBEAT_INTERVAL = 30.0


def is_postgres(session: AsyncSession) -> bool:
    """Whether this session is bound to PostgreSQL (which supports SKIP LOCKED)."""
    bind = session.get_bind()
    return bool(bind is not None and "postgres" in str(bind.dialect.name))


async def claim_next_job(
    session: AsyncSession, *, worker_id: str, now: Any | None = None
) -> BackgroundJob | None:
    """Atomically claim the highest-priority runnable job, or return ``None``.

    Ordering is ``priority DESC, created_at ASC``: higher priority wins, and
    within a priority the oldest job goes first so a burst cannot starve the
    request that arrived before it.
    """
    moment = now or utcnow()
    base = (
        select(BackgroundJob)
        .where(
            BackgroundJob.state == JobState.QUEUED.value,
            BackgroundJob.cancel_requested.is_(False),
            BackgroundJob.available_at <= moment,
        )
        .order_by(BackgroundJob.priority.desc(), BackgroundJob.created_at.asc())
        .limit(1)
    )

    if is_postgres(session):
        candidate = (await session.scalars(base.with_for_update(skip_locked=True))).first()
    else:
        candidate = (await session.scalars(base)).first()

    if candidate is None:
        return None

    if is_postgres(session):
        candidate.state = JobState.PROCESSING.value
        candidate.worker_id = worker_id
        candidate.started_at = moment
        candidate.heartbeat_at = moment
        candidate.attempts = candidate.attempts + 1
        await session.flush()
        return candidate

    # Compare-and-swap: whoever flips queued -> processing first owns the job.
    result = await session.execute(
        update(BackgroundJob)
        .where(BackgroundJob.id == candidate.id, BackgroundJob.state == JobState.QUEUED.value)
        .values(
            state=JobState.PROCESSING.value,
            worker_id=worker_id,
            started_at=moment,
            heartbeat_at=moment,
            attempts=BackgroundJob.attempts + 1,
        )
    )
    if not int(getattr(result, "rowcount", 0) or 0):
        await session.rollback()
        return None
    await session.commit()
    # ``candidate`` is already in the session's identity map with attempts=0.
    # Returning it as-is would hand the caller a stale row whose ``attempts`` is
    # permanently 0, so the retry budget would never be seen as exhausted and the
    # job would requeue forever. populate_existing forces a re-read.
    return await session.get(BackgroundJob, candidate.id, populate_existing=True)


async def _mark_cancelled_if_requested(session: AsyncSession, job_id: uuid.UUID) -> bool:
    """Report whether a cancellation was requested while the job was running."""
    requested = await session.scalar(
        select(BackgroundJob.cancel_requested).where(BackgroundJob.id == job_id)
    )
    return bool(requested)


@dataclass(slots=True)
class WorkerStats:
    """Counters exposed by ``worker.stats()`` for tests and diagnostics."""

    claimed: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    requeued: int = 0


class JobWorker:
    """A fixed pool of asyncio workers claiming jobs from the database.

    One instance is created per application process during lifespan startup and
    stopped during shutdown.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        config: Settings | None = None,
        nebius_client: Any | None = None,
        name: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config or global_settings
        self._nebius_client = nebius_client
        self._name = name or f"worker-{uuid.uuid4().hex[:8]}"
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()
        self.stats = WorkerStats()

    @property
    def name(self) -> str:
        """Identifier stamped on every job this worker claims."""
        return self._name

    @property
    def running(self) -> bool:
        """Whether the pool is currently consuming the queue."""
        return bool(self._tasks) and not self._stopping.is_set()

    async def start(self) -> None:
        """Spawn the worker coroutines. Idempotent."""
        if self._tasks:
            return
        self._stopping.clear()
        concurrency = max(1, self._config.job_worker_concurrency)
        self._tasks = [
            asyncio.create_task(self._loop(index), name=f"{self._name}-{index}")
            for index in range(concurrency)
        ]
        logger.info(
            "job worker %s started with concurrency=%s timeout=%ss",
            self._name,
            concurrency,
            self._config.job_execution_timeout_seconds,
        )

    async def stop(self) -> None:
        """Stop the pool, letting in-flight jobs finish within the drain budget.

        Jobs still queued are left in the table: another process, or the next
        boot, will pick them up. Draining rather than cancelling matters because a
        job cancelled mid-extraction would leave its document half-written.
        """
        if not self._tasks:
            return
        self._stopping.set()
        done, pending = await asyncio.wait(
            self._tasks, timeout=self._config.job_drain_timeout_seconds
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            with contextlib.suppress(asyncio.CancelledError):
                task.exception()
        self._tasks.clear()
        logger.info("job worker %s stopped", self._name)

    async def drain(self, *, max_wait: float | None = None) -> bool:
        """Block until no queued or processing job remains. Test helper.

        Returns True once the queue is empty, False if ``max_wait`` elapsed first.
        A bounded wait matters because this polls the database: on a driver that
        serialises connections, an unbounded tight loop can starve the very
        workers it is waiting on.
        """
        loop = asyncio.get_running_loop()
        deadline = None if max_wait is None else loop.time() + max_wait
        while True:
            if deadline is not None and loop.time() > deadline:
                return False
            async with self._session_factory() as session:
                pending = await session.scalar(
                    select(BackgroundJob.id)
                    .where(
                        BackgroundJob.state.in_([JobState.QUEUED.value, JobState.PROCESSING.value]),
                        BackgroundJob.cancel_requested.is_(False),
                    )
                    .limit(1)
                )
            if pending is None:
                return True
            await asyncio.sleep(0.02)

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------
    async def _loop(self, index: int) -> None:
        worker_id = f"{self._name}-{index}"
        while not self._stopping.is_set():
            try:
                processed = await self._tick(worker_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive: never kill the loop
                logger.exception("job worker %s loop error", worker_id)
                processed = False

            if not processed:
                # Nothing to do: wait for the poll interval rather than spinning
                # queries against the database.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self._config.job_poll_interval
                    )

    async def _tick(self, worker_id: str) -> bool:
        """Claim and run at most one job. Returns whether work was done."""
        async with self._session_factory() as session:
            if await reap_stale_jobs(
                session, older_than_seconds=self._config.job_stale_after_seconds
            ):
                await session.commit()
                self.stats.requeued += 1

            job = await claim_next_job(session, worker_id=worker_id)
            if job is None:
                return False
            await session.commit()
            self.stats.claimed += 1

        await self._execute(job, worker_id)
        return True

    async def _execute(self, job: BackgroundJob, worker_id: str) -> None:
        """Run one claimed job and persist its terminal state and telemetry."""
        cfg = self._config
        job_id = job.id
        handler = job_handlers.HANDLERS.get(job.job_type)
        telemetry = JobTelemetry(job_id=job_id, attempt=job.attempts)
        started = asyncio.get_running_loop().time()

        async with self._session_factory() as session:
            current = await session.get(BackgroundJob, job_id)
            if current is None:  # pragma: no cover - deleted between claim and run
                return

            if handler is None:
                await self._finish_failed(
                    session,
                    current,
                    telemetry,
                    reason=f"No handler registered for job type {current.job_type!r}.",
                    code="unknown_job_type",
                )
                self.stats.failed += 1
                return

            if current.cancel_requested:
                current.state = JobState.CANCELLED.value
                current.finished_at = utcnow()
                await record_cancelled(session, current)
                await session.commit()
                self.stats.cancelled += 1
                return

            async def progress(percent: int, message: str, **detail: Any) -> None:
                """Record a progress checkpoint.

                Progress is written through on every checkpoint so a poller sees
                real movement rather than 0% until the very end, and it doubles
                as the cancellation checkpoint.
                """
                clamped = max(0, min(100, int(percent)))
                telemetry.event("job.progress", message[:400], progress_pct=clamped, **detail)
                if await _mark_cancelled_if_requested(session, job_id):
                    raise job_handlers.JobCancelledError("Cancellation requested by client.")

                row = await session.get(BackgroundJob, job_id)
                if row is not None:
                    row.progress_pct = clamped
                    row.heartbeat_at = utcnow()
                    await session.commit()

            heartbeat = asyncio.create_task(self._heartbeat(session, job_id))
            try:
                result = await asyncio.wait_for(
                    handler(
                        current,
                        session,
                        telemetry,
                        progress,
                        nebius_client=self._nebius_client,
                        config=cfg,
                    ),
                    timeout=cfg.job_execution_timeout_seconds,
                )
            except (TimeoutError, asyncio.CancelledError) as exc:
                await session.rollback()
                current = await session.get(BackgroundJob, job_id)
                if current is None:  # pragma: no cover - deleted mid-execution
                    return
                if await _mark_cancelled_if_requested(session, job_id):
                    await self._finish_cancelled(session, current, telemetry)
                    self.stats.cancelled += 1
                else:
                    reason = (
                        f"Exceeded {cfg.job_execution_timeout_seconds:.0f}s execution timeout."
                        if isinstance(exc, TimeoutError)
                        else "Worker shut down mid-job."
                    )
                    await self._finish_failed(
                        session, current, telemetry, reason=reason, code="timeout"
                    )
                    self.stats.failed += 1
                return
            except job_handlers.JobCancelledError as exc:
                await session.rollback()
                current = await session.get(BackgroundJob, job_id)
                if current is not None:
                    await self._finish_cancelled(session, current, telemetry, reason=str(exc))
                    self.stats.cancelled += 1
                return
            except Exception as exc:
                await session.rollback()
                current = await session.get(BackgroundJob, job_id)
                if current is not None:
                    await self._finish_failed(
                        session,
                        current,
                        telemetry,
                        reason=str(exc) or exc.__class__.__name__,
                        code=getattr(exc, "code", "exception"),
                    )
                    self.stats.failed += 1
                return
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

            current = await session.get(BackgroundJob, job_id)
            if current is None:  # pragma: no cover
                return

            duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            summary = await flush(
                session,
                telemetry,
                job_type=current.job_type,
                state=JobState.COMPLETED,
                max_entries=cfg.job_max_log_entries,
            )
            current.state = JobState.COMPLETED.value
            current.progress_pct = 100
            current.finished_at = utcnow()
            current.duration_ms = duration_ms
            current.heartbeat_at = utcnow()
            current.result = {**summary, **(result or {})}
            current.error = None
            current.error_code = None
            current.worker_id = None
            current.nebius_job_id = current.nebius_job_id or f"ntask_{job_id.hex[:24]}"
            await session.commit()
            self.stats.completed += 1

    async def _heartbeat(self, session: AsyncSession, job_id: uuid.UUID) -> None:
        """Prove liveness while a job runs so it is never mistaken for stale."""
        try:
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                row = await session.get(BackgroundJob, job_id)
                if row is None:
                    return
                row.heartbeat_at = utcnow()
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - heartbeat is best effort
            logger.debug("heartbeat for job %s stopped", job_id, exc_info=True)

    async def _finish_failed(
        self,
        session: AsyncSession,
        job: BackgroundJob,
        telemetry: JobTelemetry,
        *,
        reason: str,
        code: str,
    ) -> None:
        """Mark a job failed, or requeue it when attempts remain.

        A transient provider error is worth retrying; a malformed payload is not.
        Permanent codes requeue onto the same schedule and simply burn the retry
        budget, which keeps the retry decision in one place instead of splitting
        the policy across every handler.
        """
        cfg = self._config
        exhausted = job.attempts >= job.max_attempts
        if exhausted:
            state = JobState.FAILED
        else:
            state = JobState.QUEUED

        job.error = reason[:2000]
        job.error_code = code[:64]
        job.worker_id = None
        job.heartbeat_at = None
        job.progress_pct = 0
        job.duration_ms = telemetry.elapsed_ms()
        job.state = state.value
        # started_at is deliberately kept: a failed job's wall-clock duration is
        # exactly what an operator needs when diagnosing it. claim_next_job
        # overwrites the field when the next attempt begins.
        if state is JobState.QUEUED:
            # Exponential backoff so a provider outage does not become a hot
            # retry loop against an already-sick dependency.
            delay = cfg.job_retry_backoff_seconds * max(1, job.attempts)
            job.available_at = utcnow() + timedelta(seconds=delay)
        else:
            job.finished_at = utcnow()

        await flush(
            session,
            telemetry,
            job_type=job.job_type,
            state=JobState.FAILED,
            max_entries=cfg.job_max_log_entries,
        )
        await session.commit()
        logger.warning(
            "job %s failed attempt=%s/%s code=%s requeued=%s error=%s",
            job.id,
            job.attempts,
            job.max_attempts,
            code,
            state is JobState.QUEUED,
            reason,
        )

    async def _finish_cancelled(
        self,
        session: AsyncSession,
        job: BackgroundJob,
        telemetry: JobTelemetry,
        *,
        reason: str = "cancelled by request",
    ) -> None:
        """Stop a job that was cancelled mid-flight."""
        job.state = JobState.CANCELLED.value
        job.finished_at = utcnow()
        job.duration_ms = telemetry.elapsed_ms()
        job.worker_id = None
        job.cancel_requested = True
        telemetry.event("job.cancelled", reason, level="warning")
        await flush(
            session,
            telemetry,
            job_type=job.job_type,
            state=JobState.CANCELLED,
            max_entries=self._config.job_max_log_entries,
        )
        await session.commit()


__all__ = ["JobWorker", "WorkerStats", "claim_next_job", "is_postgres"]
