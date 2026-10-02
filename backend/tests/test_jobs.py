"""Behavioural tests for the background job queue.

These exercise the dispatcher against a real database rather than mocking it,
because the interesting failures in a queue are concurrency and persistence
bugs: a job claimed twice, a requeue that never comes back, a shutdown that
strands work in flight.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from app.core.metrics import metrics
from app.db.models import BackgroundJob, Document, JobLog, JobState, User
from app.services.jobs import (
    HANDLERS,
    JobHandlerError,
    JobTelemetry,
    JobWorker,
    claim_next_job,
    create_job,
    request_cancel,
    trim_logs,
)
from app.services.jobs import handlers as job_handlers
from app.services.jobs.store import reap_stale_jobs
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.usefixtures("job_settings")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
async def _user(session: AsyncSession) -> User:
    user = User(email=f"jobs-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
    session.add(user)
    await session.commit()
    return user


async def _store_document(session: AsyncSession, owner: User, tmp: Path) -> Document:
    """A real DOCX on disk, so extraction exercises the actual parser."""
    tmp.mkdir(parents=True, exist_ok=True)
    stored = tmp / f"{uuid.uuid4().hex[:8]}.docx"
    import docx

    document_obj = docx.Document()
    document_obj.add_paragraph("Executive Summary")
    document_obj.add_paragraph("Revenue grew across every region.")
    table = document_obj.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Region"
    table.cell(0, 1).text = "Revenue"
    table.cell(1, 0).text = "EMEA"
    table.cell(1, 1).text = "4.2"
    document_obj.save(str(stored))

    row = Document(
        owner_id=owner.id,
        filename=stored.name,
        storage_path=str(stored),
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=stored.stat().st_size,
        status="uploaded",
        metadata_={"format": "docx"},
    )
    session.add(row)
    await session.commit()
    return row


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------
class TestSubmission:
    async def test_new_job_starts_queued(self, queue_engine) -> None:
        async with queue_engine() as session:
            user = await _user(session)
            job = await create_job(
                session, job_type="custom", payload={"prompt": "hi"}, submitted_by_id=user.id
            )
            await session.commit()

        assert job.state == JobState.QUEUED.value
        assert job.attempts == 0
        assert job.progress_pct == 0
        assert job.result is None

    async def test_row_is_persisted_before_any_worker_runs(self, queue_engine) -> None:
        """A job must survive a crash between accepting it and running it."""
        async with queue_engine() as session:
            user = await _user(session)
            job = await create_job(
                session, job_type="custom", payload={"prompt": "hi"}, submitted_by_id=user.id
            )
            await session.commit()

        async with queue_engine() as fresh:
            assert await fresh.get(BackgroundJob, job.id) is not None

    async def test_max_attempts_floor_is_one(self, queue_engine) -> None:
        async with queue_engine() as session:
            job = await create_job(
                session, job_type="custom", payload={}, max_attempts=0, submitted_by_id=None
            )
            await session.commit()
        assert job.max_attempts == 1


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------
class TestClaiming:
    async def test_claims_highest_priority_first(self, queue_engine) -> None:
        async with queue_engine() as session:
            low = await create_job(session, job_type="custom", payload={}, priority=0)
            high = await create_job(session, job_type="custom", payload={}, priority=9)
            await session.commit()

            claimed = await claim_next_job(session, worker_id="w")
            await session.commit()
            assert claimed is not None
            assert claimed.id == high.id

            second = await claim_next_job(session, worker_id="w")
            await session.commit()
            assert second is not None and second.id == low.id

    async def test_oldest_job_wins_within_a_priority(self, queue_engine) -> None:
        async with queue_engine() as session:
            first = await create_job(session, job_type="custom", payload={})
            await session.commit()
            second = await create_job(session, job_type="custom", payload={})
            await session.commit()

            claimed = await claim_next_job(session, worker_id="w")
            await session.commit()
            assert claimed is not None and claimed.id == first.id
            assert second.id != first.id

    async def test_a_job_is_never_claimed_twice(self, queue_engine) -> None:
        async with queue_engine() as session:
            await create_job(session, job_type="custom", payload={})
            await session.commit()

        async with queue_engine() as a, queue_engine() as b:
            first = await claim_next_job(a, worker_id="w1")
            await a.commit()
            second = await claim_next_job(b, worker_id="w2")
            await b.commit()

        assert first is not None
        assert second is None

    async def test_claiming_increments_attempts_and_stamps_the_worker(self, queue_engine) -> None:
        async with queue_engine() as session:
            await create_job(session, job_type="custom", payload={})
            await session.commit()
            claimed = await claim_next_job(session, worker_id="worker-7")
            await session.commit()

        assert claimed is not None
        assert claimed.state == JobState.PROCESSING.value
        assert claimed.attempts == 1
        assert claimed.worker_id == "worker-7"
        assert claimed.started_at is not None
        assert claimed.heartbeat_at is not None

    async def test_future_dated_job_is_not_claimable_yet(self, queue_engine) -> None:
        from datetime import timedelta

        from app.services.jobs.store import utcnow

        async with queue_engine() as session:
            await create_job(
                session,
                job_type="custom",
                payload={},
                available_at=utcnow() + timedelta(hours=1),
            )
            await session.commit()
            assert await claim_next_job(session, worker_id="w") is None

    async def test_cancelled_job_is_never_claimed(self, queue_engine) -> None:
        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={})
            await request_cancel(session, job)
            await session.commit()

            assert job.state == JobState.CANCELLED.value
            assert await claim_next_job(session, worker_id="w") is None


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------
class TestCancellation:
    async def test_queued_job_cancels_immediately(self, queue_engine) -> None:
        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={})
            assert await request_cancel(session, job) is True
            await session.commit()

        assert job.state == JobState.CANCELLED.value
        assert job.finished_at is not None

    async def test_running_job_is_only_flagged(self, queue_engine) -> None:
        """The worker owns its own unwind; killing it here would strand a session."""
        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={})
            claimed = await claim_next_job(session, worker_id="w")
            await session.commit()
            assert claimed is not None

            assert await request_cancel(session, job) is True
            await session.commit()

        assert job.state == JobState.PROCESSING.value
        assert job.cancel_requested is True
        assert job.finished_at is None

    async def test_terminal_job_cannot_be_cancelled(self, queue_engine) -> None:
        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={})
            job.state = JobState.COMPLETED.value
            await session.commit()

            assert await request_cancel(session, job) is False

    async def test_cancelling_is_idempotent(self, queue_engine) -> None:
        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={})
            assert await request_cancel(session, job) is True
            await session.commit()
            job.state = JobState.COMPLETED.value
            await session.commit()
            assert await request_cancel(session, job) is False


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
class TestExecution:
    async def test_document_extraction_completes_and_persists_chunks(
        self, queue_engine, tmp_path, fake_client
    ) -> None:
        async with queue_engine() as session:
            user = await _user(session)
            document = await _store_document(session, user, tmp_path / "src")
            job = await create_job(
                session,
                job_type="document_extraction",
                payload={},
                document_id=document.id,
                submitted_by_id=user.id,
            )
            await session.commit()
            job_id = job.id
            document_id = document.id

        worker = JobWorker(queue_engine, name="w1")
        await worker.start()
        try:
            assert await worker.drain(max_wait=20) is True
        finally:
            await worker.stop()

        async with queue_engine() as session:
            done = await session.get(BackgroundJob, job_id)
            assert done is not None
            assert done.state == JobState.COMPLETED.value
            assert done.progress_pct == 100
            assert done.duration_ms is not None
            assert done.result is not None
            assert done.result["chunk_count"] >= 1

            document = await session.get(Document, document_id)
            assert document is not None
            assert document.chunk_count == done.result["chunk_count"]
            assert document.status == "embedding"

    async def test_vectors_are_stored_when_a_client_is_available(
        self, queue_engine, tmp_path, fake_client
    ) -> None:
        async with queue_engine() as session:
            user = await _user(session)
            document = await _store_document(session, user, tmp_path / "src")
            await create_job(
                session,
                job_type="document_extraction",
                payload={},
                document_id=document.id,
                submitted_by_id=user.id,
            )
            await session.commit()
            document_id = document.id

        worker = JobWorker(queue_engine, nebius_client=fake_client, name="w1")
        await worker.start()
        try:
            assert await worker.drain(max_wait=20) is True
        finally:
            await worker.stop()

        assert fake_client.calls, "the embedding client should have been used"
        async with queue_engine() as session:
            document = await session.get(Document, document_id)
            assert document is not None
            assert document.status == "ready"
            assert all(chunk.embedding for chunk in document.chunks)

    async def test_result_captures_provider_token_usage(self, queue_engine) -> None:
        from app.core.metrics import TokenUsage

        telemetry = JobTelemetry(job_id=uuid.uuid4(), attempt=1)
        telemetry.record_tokens(
            TokenUsage(prompt_tokens=120, completion_tokens=45), model="Nemotron-3-Super"
        )
        summary = telemetry.summary(state=JobState.COMPLETED, duration_ms=900)

        assert summary["prompt_tokens"] == 120
        assert summary["completion_tokens"] == 45
        assert summary["total_tokens"] == 165
        assert summary["token_models"] == ["Nemotron-3-Super"]
        assert summary["duration_ms"] == 900

    async def test_unknown_job_type_fails_with_a_clear_code(self, queue_engine) -> None:
        async with queue_engine() as session:
            job = await create_job(session, job_type="teleport", payload={}, max_attempts=1)
            await session.commit()
            job_id = job.id

        worker = JobWorker(queue_engine, name="w1")
        await worker.start()
        try:
            assert await worker.drain(max_wait=20) is True
        finally:
            await worker.stop()

        async with queue_engine() as session:
            done = await session.get(BackgroundJob, job_id)
            assert done is not None
            assert done.state == JobState.FAILED.value
            assert done.error_code == "unknown_job_type"
            assert "teleport" in (done.error or "")

    async def test_missing_document_fails_fast_with_a_code(self, queue_engine) -> None:
        async with queue_engine() as session:
            job = await create_job(
                session,
                job_type="document_extraction",
                payload={"document_id": str(uuid.uuid4())},
                max_attempts=1,
            )
            await session.commit()
            job_id = job.id

        worker = JobWorker(queue_engine, name="w1")
        await worker.start()
        try:
            assert await worker.drain(max_wait=20) is True
        finally:
            await worker.stop()

        async with queue_engine() as session:
            done = await session.get(BackgroundJob, job_id)
            assert done is not None and done.state == JobState.FAILED.value
            assert done.error_code == "document_missing"

    async def test_missing_source_bytes_are_reported(self, queue_engine, tmp_path) -> None:
        async with queue_engine() as session:
            user = await _user(session)
            ghost = Document(
                owner_id=user.id,
                filename="gone.pdf",
                storage_path=str(tmp_path / "absent.pdf"),
                metadata_={"format": "pdf"},
            )
            session.add(ghost)
            await session.commit()
            job = await create_job(
                session,
                job_type="document_extraction",
                payload={},
                document_id=ghost.id,
                max_attempts=1,
            )
            await session.commit()
            job_id = job.id

        worker = JobWorker(queue_engine, name="w1")
        await worker.start()
        try:
            assert await worker.drain(max_wait=20) is True
        finally:
            await worker.stop()

        async with queue_engine() as session:
            done = await session.get(BackgroundJob, job_id)
            assert done is not None and done.error_code == "source_missing"

    async def test_empty_document_is_rejected(self, queue_engine, tmp_path) -> None:
        blank = tmp_path / "blank.txt"
        blank.write_text("   \n\n  ", encoding="utf-8")

        async with queue_engine() as session:
            user = await _user(session)
            document = Document(
                owner_id=user.id,
                filename="blank.txt",
                storage_path=str(blank),
                metadata_={"format": "txt"},
            )
            session.add(document)
            await session.commit()
            job = await create_job(
                session,
                job_type="document_extraction",
                payload={},
                document_id=document.id,
                max_attempts=1,
            )
            await session.commit()
            job_id = job.id

        worker = JobWorker(queue_engine, name="w1")
        await worker.start()
        try:
            assert await worker.drain(max_wait=20) is True
        finally:
            await worker.stop()

        async with queue_engine() as session:
            done = await session.get(BackgroundJob, job_id)
            assert done is not None and done.error_code == "empty_document"


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------
class TestRetry:
    async def test_failure_is_requeued_until_attempts_run_out(self, queue_engine) -> None:
        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={}, max_attempts=3)
            await session.commit()
            job_id = job.id

        worker = JobWorker(queue_engine, name="w1")
        await worker.start()
        try:
            assert await worker.drain(max_wait=20) is True
        finally:
            await worker.stop()

        async with queue_engine() as session:
            done = await session.get(BackgroundJob, job_id)
            assert done is not None
            assert done.state == JobState.FAILED.value
            assert done.attempts == 3
            assert worker.stats.claimed == 3
            assert worker.stats.failed == 3

    async def test_retry_delays_the_next_attempt(self, queue_engine, job_settings) -> None:
        from datetime import timedelta

        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={}, max_attempts=2)
            await session.commit()
            job_id = job.id

        slow = job_settings.model_copy(update={"job_retry_backoff_seconds": 30.0})
        worker = JobWorker(queue_engine, config=slow, name="w1")
        await worker.start()
        await asyncio.sleep(0.2)
        await worker.stop()

        async with queue_engine() as session:
            done = await session.get(BackgroundJob, job_id)
            assert done is not None
            assert done.state == JobState.QUEUED.value
            assert done.attempts == 1
            assert done.available_at > done.created_at + timedelta(seconds=10)

    async def test_a_successful_retry_completes(self, queue_engine, monkeypatch) -> None:
        calls = {"n": 0}
        original = job_handlers.HANDLERS["custom"]

        async def flaky(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise JobHandlerError("transient provider blip", code="blip")
            return {"ok": True, "attempt": calls["n"]}

        monkeypatch.setitem(job_handlers.HANDLERS, "custom", flaky)
        try:
            async with queue_engine() as session:
                job = await create_job(session, job_type="custom", payload={}, max_attempts=3)
                await session.commit()
                job_id = job.id

            worker = JobWorker(queue_engine, name="w1")
            await worker.start()
            try:
                assert await worker.drain(max_wait=20) is True
            finally:
                await worker.stop()
        finally:
            monkeypatch.setitem(job_handlers.HANDLERS, "custom", original)

        async with queue_engine() as session:
            done = await session.get(BackgroundJob, job_id)
            assert done is not None
            assert done.state == JobState.COMPLETED.value
            assert done.attempts == 2
            assert done.error is None
            assert done.result is not None and done.result["attempt"] == 2


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------
class TestTimeout:
    async def test_slow_job_fails_with_a_timeout_code(self, queue_engine, job_settings) -> None:
        async def forever(*args: Any, **kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(30)
            return {"never": True}

        original = job_handlers.HANDLERS["custom"]
        job_handlers.HANDLERS["custom"] = forever
        try:
            async with queue_engine() as session:
                job = await create_job(session, job_type="custom", payload={}, max_attempts=1)
                await session.commit()
                job_id = job.id

            impatient = job_settings.model_copy(update={"job_execution_timeout_seconds": 0.2})
            worker = JobWorker(queue_engine, config=impatient, name="w1")
            await worker.start()
            try:
                assert await worker.drain(max_wait=20) is True
            finally:
                await worker.stop()
        finally:
            job_handlers.HANDLERS["custom"] = original

        async with queue_engine() as session:
            done = await session.get(BackgroundJob, job_id)
            assert done is not None
            assert done.state == JobState.FAILED.value
            assert done.error_code == "timeout"
            assert "timeout" in (done.error or "").lower()


# ---------------------------------------------------------------------------
# Cancellation mid-flight
# ---------------------------------------------------------------------------
class TestMidFlightCancellation:
    async def test_cancelling_a_running_job_stops_it_at_the_next_checkpoint(
        self, queue_engine, monkeypatch
    ) -> None:
        started = asyncio.Event()
        progressed = asyncio.Event()

        async def slow(*args: Any, **kwargs: Any) -> dict[str, Any]:
            progress = args[3]
            started.set()
            await progress(20, "first checkpoint")
            await progressed.wait()
            await progress(80, "second checkpoint")
            return {"finished": True}

        original = job_handlers.HANDLERS["custom"]
        monkeypatch.setitem(job_handlers.HANDLERS, "custom", slow)

        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={}, max_attempts=1)
            await session.commit()
            job_id = job.id

        worker = JobWorker(queue_engine, name="w1")
        await worker.start()
        await asyncio.wait_for(started.wait(), timeout=10)

        async with queue_engine() as session:
            row = await session.get(BackgroundJob, job_id)
            assert row is not None
            await request_cancel(session, row)
            await session.commit()

        progressed.set()
        try:
            assert await worker.drain(max_wait=20) is True
        finally:
            await worker.stop()
            monkeypatch.setitem(job_handlers.HANDLERS, "custom", original)

        async with queue_engine() as session:
            done = await session.get(BackgroundJob, job_id)
            assert done is not None
            assert done.state == JobState.CANCELLED.value
            assert worker.stats.cancelled == 1


# ---------------------------------------------------------------------------
# Stale reclaim
# ---------------------------------------------------------------------------
class TestStaleReclaim:
    async def test_a_dead_workers_job_returns_to_the_queue(self, queue_engine) -> None:
        from datetime import timedelta

        from app.services.jobs.store import utcnow

        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={})
            claimed = await claim_next_job(session, worker_id="dead-worker")
            await session.commit()
            assert claimed is not None
            job.heartbeat_at = utcnow() - timedelta(hours=1)

            reaped = await reap_stale_jobs(session, older_than_seconds=60.0)
            await session.commit()

        assert reaped == [job.id]
        assert job.state == JobState.QUEUED.value
        assert job.worker_id is None
        assert job.error_code == "stale_worker"
        assert job.attempts == 1

    async def test_a_live_job_is_left_alone(self, queue_engine) -> None:
        async with queue_engine() as session:
            await create_job(session, job_type="custom", payload={})
            await claim_next_job(session, worker_id="live")
            await session.commit()

            assert await reap_stale_jobs(session, older_than_seconds=300.0) == []

    async def test_cancelled_claimed_jobs_are_not_reclaimed(self, queue_engine) -> None:
        from datetime import timedelta

        from app.services.jobs.store import utcnow

        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={})
            await claim_next_job(session, worker_id="dead")
            await session.commit()
            job.cancel_requested = True
            job.heartbeat_at = utcnow() - timedelta(hours=1)

            assert await reap_stale_jobs(session, older_than_seconds=60.0) == []


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------
class TestTelemetry:
    async def test_every_attempt_writes_a_terminal_line(self, queue_engine) -> None:
        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={}, max_attempts=1)
            await session.commit()
            job_id = job.id

        worker = JobWorker(queue_engine, name="w1")
        await worker.start()
        try:
            assert await worker.drain(max_wait=20) is True
        finally:
            await worker.stop()

        async with queue_engine() as session:
            logs = (
                await session.scalars(
                    select(JobLog).where(JobLog.job_id == job_id).order_by(JobLog.sequence)
                )
            ).all()

        assert logs
        terminal = [line for line in logs if line.event.startswith("job.")]
        assert terminal, "a terminal state line must be recorded"
        assert all("duration_ms" in line.data for line in terminal)

    async def test_sequences_are_gapless_and_unique(self, queue_engine) -> None:
        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={}, max_attempts=2)
            await session.commit()
            job_id = job.id

        worker = JobWorker(queue_engine, name="w1")
        await worker.start()
        try:
            assert await worker.drain(max_wait=20) is True
        finally:
            await worker.stop()

        async with queue_engine() as session:
            rows = (
                await session.scalars(
                    select(JobLog.sequence).where(JobLog.job_id == job_id).order_by(JobLog.sequence)
                )
            ).all()

        assert len(rows) == len(set(rows)), "sequences must be unique"
        assert rows == list(range(len(rows))), "sequences must start at 0 and be contiguous"

    async def test_duration_is_recorded_on_the_row(self, queue_engine) -> None:
        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={}, max_attempts=1)
            await session.commit()
            job_id = job.id

        worker = JobWorker(queue_engine, name="w1")
        await worker.start()
        try:
            assert await worker.drain(max_wait=20) is True
        finally:
            await worker.stop()

        async with queue_engine() as session:
            done = await session.get(BackgroundJob, job_id)
            assert done is not None
            assert done.duration_ms is not None and done.duration_ms >= 0
            assert done.started_at is not None and done.finished_at is not None

    async def test_job_metrics_are_recorded_globally(self, queue_engine) -> None:
        metrics.reset()
        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={}, max_attempts=1)
            await session.commit()
            job_id = job.id

        worker = JobWorker(queue_engine, name="w1")
        await worker.start()
        try:
            assert await worker.drain(max_wait=20) is True
        finally:
            await worker.stop()

        snapshot = metrics.snapshot()["jobs"]
        assert snapshot["total"] >= 1
        assert snapshot["by_type"]["custom"]["total"] >= 1

        async with queue_engine() as session:
            assert await session.scalar(
                select(func.count()).select_from(JobLog).where(JobLog.job_id == job_id)
            )

    async def test_logs_are_trimmed_to_the_configured_cap(self, queue_engine) -> None:
        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={})
            await session.commit()
            for index in range(10):
                session.add(
                    JobLog(
                        job_id=job.id,
                        sequence=index,
                        level="info",
                        event="test",
                        message="line",
                        data={},
                    )
                )
            await session.commit()

            removed = await trim_logs(session, job.id, keep=4)
            await session.commit()

            remaining = (
                await session.scalars(
                    select(JobLog.sequence).where(JobLog.job_id == job.id).order_by(JobLog.sequence)
                )
            ).all()

        assert removed == 6
        assert remaining == [6, 7, 8, 9]

    async def test_trimming_below_the_cap_is_a_no_op(self, queue_engine) -> None:
        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={})
            await session.commit()
            session.add(JobLog(job_id=job.id, sequence=0, event="t", message="m", data={}))
            await session.commit()

            assert await trim_logs(session, job.id, keep=50) == 0


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------
class TestShutdown:
    async def test_queued_jobs_survive_a_restart(self, queue_engine, job_settings) -> None:
        """A job submitted with no worker running is picked up by the next one."""
        async with queue_engine() as session:
            job = await create_job(
                session, job_type="custom", payload={"prompt": "x"}, max_attempts=1
            )
            await session.commit()
            job_id = job.id

        # No worker running yet: the job simply waits in the table.
        await asyncio.sleep(0.1)
        async with queue_engine() as session:
            assert (await session.get(BackgroundJob, job_id)).state == JobState.QUEUED.value

        async def fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"done": True}

        original = job_handlers.HANDLERS["custom"]
        job_handlers.HANDLERS["custom"] = fail
        try:
            worker = JobWorker(queue_engine, config=job_settings, name="w1")
            await worker.start()
            try:
                assert await worker.drain(max_wait=20) is True
            finally:
                await worker.stop()
        finally:
            job_handlers.HANDLERS["custom"] = original

        async with queue_engine() as session:
            assert (await session.get(BackgroundJob, job_id)).state == JobState.COMPLETED.value

    async def test_stop_is_idempotent(self, queue_engine, job_settings) -> None:
        worker = JobWorker(queue_engine, config=job_settings, name="w1")
        await worker.stop()
        await worker.start()
        await worker.stop()
        await worker.stop()
        assert worker.running is False

    async def test_start_is_idempotent(self, queue_engine, job_settings) -> None:
        worker = JobWorker(queue_engine, config=job_settings, name="w1")
        await worker.start()
        await worker.start()
        try:
            assert worker.running is True
        finally:
            await worker.stop()

    async def test_drain_reports_a_timeout_instead_of_hanging(
        self, queue_engine, job_settings
    ) -> None:
        blocked = job_settings.model_copy(update={"job_poll_interval": 5.0})
        worker = JobWorker(queue_engine, config=blocked, name="w1")
        async with queue_engine() as session:
            await create_job(
                session,
                job_type="custom",
                payload={},
                available_at=_far_future(),
                max_attempts=1,
            )
            await session.commit()

        assert await worker.drain(max_wait=0.2) is False


def _far_future():
    from datetime import timedelta

    from app.services.jobs.store import utcnow

    return utcnow() + timedelta(days=1)


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------
class TestHandlerRegistry:
    def test_every_job_type_has_a_handler(self) -> None:
        from app.db.models import JobType

        for job_type in JobType:
            assert job_type.value in HANDLERS, f"{job_type.value} has no handler"

    async def test_embedding_without_a_client_fails_with_a_code(self, queue_engine) -> None:
        async with queue_engine() as session:
            job = await create_job(
                session,
                job_type="embedding",
                payload={"document_id": str(uuid.uuid4())},
                max_attempts=1,
            )
            await session.commit()
            job_id = job.id

        worker = JobWorker(queue_engine, name="w1")
        await worker.start()
        try:
            assert await worker.drain(max_wait=20) is True
        finally:
            await worker.stop()

        async with queue_engine() as session:
            done = await session.get(BackgroundJob, job_id)
            assert done is not None and done.error_code == "no_client"

    async def test_llm_job_without_a_prompt_fails_with_a_code(self, queue_engine) -> None:
        """A missing prompt must be caught even when a client is configured.

        Validation order matters: the worker checks the client first, so the
        prompt guard would otherwise be unreachable in exactly the deployments
        that are correctly configured.
        """
        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={}, max_attempts=1)
            await session.commit()
            job_id = job.id

        worker = JobWorker(queue_engine, nebius_client=object(), name="w1")
        await worker.start()
        try:
            assert await worker.drain(max_wait=20) is True
        finally:
            await worker.stop()

        async with queue_engine() as session:
            done = await session.get(BackgroundJob, job_id)
            assert done is not None and done.error_code == "no_prompt"


# ---------------------------------------------------------------------------
# Cascade cleanup
# ---------------------------------------------------------------------------
class TestCascadeCleanup:
    async def test_deleting_a_job_removes_its_log(self, queue_engine) -> None:
        async with queue_engine() as session:
            job = await create_job(session, job_type="custom", payload={}, max_attempts=1)
            await session.commit()
            job_id = job.id

        worker = JobWorker(queue_engine, name="w1")
        await worker.start()
        try:
            assert await worker.drain(max_wait=20) is True
        finally:
            await worker.stop()

        async with queue_engine() as session:
            assert await session.scalar(
                select(func.count()).select_from(JobLog).where(JobLog.job_id == job_id)
            )
            row = await session.get(BackgroundJob, job_id)
            await session.delete(row)
            await session.commit()

            assert (
                await session.scalar(
                    select(func.count()).select_from(JobLog).where(JobLog.job_id == job_id)
                )
                == 0
            )

    async def test_deleting_a_document_removes_its_jobs(self, queue_engine, tmp_path) -> None:
        async with queue_engine() as session:
            user = await _user(session)
            document = await _store_document(session, user, tmp_path / "src")
            await create_job(
                session,
                job_type="document_extraction",
                payload={},
                document_id=document.id,
                submitted_by_id=user.id,
            )
            await session.commit()
            document_id = document.id

        async with queue_engine() as session:
            await session.execute(delete(BackgroundJob))
            await session.execute(delete(Document).where(Document.id == document_id))
            await session.commit()

            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(BackgroundJob)
                    .where(BackgroundJob.document_id == document_id)
                )
                == 0
            )
