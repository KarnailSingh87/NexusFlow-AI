"""HTTP-level tests for the background job endpoints.

The queue itself is covered in ``test_jobs.py``. These exercise the contract a
client actually sees: the submit response, ownership scoping, the 409 that stops
a caller reading a result before it exists, and the log cursor.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_config
from app.core.config import Settings
from app.db.models import BackgroundJob, JobLog, JobState, User
from app.services.jobs import JobWorker, create_job

pytestmark = pytest.mark.usefixtures("job_settings")

USER = "jobs-api@example.com"
OTHER_USER = "mallory@example.com"


@pytest.fixture
async def api(app: Any, queue_engine: async_sessionmaker[AsyncSession]) -> Any:
    """Wire the app to the queue database and hand back an HTTP client."""
    import httpx

    job_cfg = Settings(**{}).model_copy(
        update={
            "job_poll_interval": 0.01,
            "job_retry_backoff_seconds": 0.01,
            "job_drain_timeout_seconds": 5.0,
        }
    )

    async def _config() -> Settings:
        return job_cfg

    app.dependency_overrides[get_config] = _config

    async def _session() -> Any:
        async with queue_engine() as session:
            yield session

    app.dependency_overrides[__import__(
        "app.db.session", fromlist=["get_session"]
    ).get_session] = _session
    app.state.job_worker = None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client
    app.dependency_overrides.clear()


async def _submit(client: Any, **body: Any) -> dict[str, Any]:
    payload = {"job_type": "custom", "payload": {"prompt": "analyse"}, **body}
    response = await client.post("/api/v1/jobs", json=payload, headers=_h(USER))
    assert response.status_code == 202, response.text
    return response.json()


def _h(email: str) -> dict[str, str]:
    return {"X-User-Email": email}


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------
class TestSubmit:
    async def test_submit_returns_202_with_a_poll_handle(self, api: Any) -> None:
        body = await _submit(api)
        assert uuid.UUID(body["id"])
        assert body["state"] == "queued"
        assert body["job_type"] == "custom"
        assert body["available_at"]

    async def test_submit_is_fast_and_does_not_run_the_work(self, api: Any) -> None:
        """The whole point: submitting must not block on the workload."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        await _submit(api)
        assert loop.time() - started < 0.5

    async def test_duplicate_submission_creates_two_jobs(self, api: Any) -> None:
        first = await _submit(api)
        second = await _submit(api)
        assert first["id"] != second["id"]

    async def test_priority_is_persisted(self, api: Any) -> None:
        body = await _submit(api, priority=7)
        assert body["priority"] == 7

    async def test_unknown_job_type_is_rejected(self, api: Any) -> None:
        response = await api.post(
            "/api/v1/jobs",
            json={"job_type": "teleport", "payload": {}},
            headers=_h(USER),
        )
        assert response.status_code == 422

    async def test_llm_job_without_a_prompt_is_rejected_at_the_edge(self, api: Any) -> None:
        """Fails as 422 rather than queueing a job that can only fail."""
        response = await api.post(
            "/api/v1/jobs",
            json={"job_type": "custom", "payload": {}},
            headers=_h(USER),
        )
        assert response.status_code == 422
        assert "prompt" in response.text

    async def test_document_job_without_an_id_is_rejected(self, api: Any) -> None:
        response = await api.post(
            "/api/v1/jobs",
            json={"job_type": "document_extraction", "payload": {}},
            headers=_h(USER),
        )
        assert response.status_code == 422

    async def test_unknown_field_is_rejected(self, api: Any) -> None:
        response = await api.post(
            "/api/v1/jobs",
            json={"job_type": "custom", "payload": {"prompt": "x"}, "surprise": 1},
            headers=_h(USER),
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Poll
# ---------------------------------------------------------------------------
class TestPoll:
    async def test_status_reports_the_queued_state(self, api: Any) -> None:
        job = await _submit(api)
        response = await api.get(f"/api/v1/jobs/{job['id']}", headers=_h(USER))
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "queued"
        assert body["progress_pct"] == 0
        assert body["attempts"] == 0
        assert body["finished_at"] is None

    async def test_unknown_job_is_404(self, api: Any) -> None:
        response = await api.get(f"/api/v1/jobs/{uuid.uuid4()}", headers=_h(USER))
        assert response.status_code == 404

    async def test_another_users_job_is_404_not_403(self, api: Any) -> None:
        """Confirming an id exists is itself a leak."""
        job = await _submit(api)
        response = await api.get(f"/api/v1/jobs/{job['id']}", headers=_h(OTHER_USER))
        assert response.status_code == 404

    async def test_list_returns_only_your_jobs(self, api: Any) -> None:
        await _submit(api)
        mine = await api.get("/api/v1/jobs", headers=_h(USER))
        theirs = await api.get("/api/v1/jobs", headers=_h(OTHER_USER))
        assert mine.json()["total"] == 1
        assert theirs.json()["total"] == 0

    async def test_list_can_filter_by_state(self, api: Any) -> None:
        await _submit(api)
        queued = await api.get("/api/v1/jobs?state=queued", headers=_h(USER))
        completed = await api.get("/api/v1/jobs?state=completed", headers=_h(USER))
        assert queued.json()["total"] == 1
        assert completed.json()["total"] == 0

    async def test_list_rejects_an_unknown_state(self, api: Any) -> None:
        response = await api.get("/api/v1/jobs?state=exploded", headers=_h(USER))
        assert response.status_code == 422

    async def test_queue_stats_report_depth_and_worker_state(self, api: Any) -> None:
        await _submit(api)
        response = await api.get("/api/v1/jobs/queue", headers=_h(USER))
        assert response.status_code == 200
        body = response.json()
        assert body["queued"] == 1
        assert body["worker_running"] is False


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
class TestResult:
    async def test_result_before_completion_is_409(self, api: Any) -> None:
        """An empty 200 would let a caller read 'not done' as 'produced nothing'."""
        job = await _submit(api)
        response = await api.get(f"/api/v1/jobs/{job['id']}/result", headers=_h(USER))
        assert response.status_code == 409
        assert "queued" in response.text

    async def test_result_is_returned_once_complete(
        self, api: Any, queue_engine: async_sessionmaker[AsyncSession]
    ) -> None:
        job = await _submit(api)

        async def succeed(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"answer": 42, "model": "Nemotron-3-Super"}

        from app.services.jobs import handlers as job_handlers

        original = job_handlers.HANDLERS["custom"]
        job_handlers.HANDLERS["custom"] = succeed
        worker = JobWorker(queue_engine, name="api-worker")
        await worker.start()
        try:
            assert await worker.drain(max_wait=20) is True
        finally:
            await worker.stop()
            job_handlers.HANDLERS["custom"] = original

        response = await api.get(f"/api/v1/jobs/{job['id']}/result", headers=_h(USER))
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "completed"
        assert body["data"]["answer"] == 42
        assert body["data"]["model"] == "Nemotron-3-Super"
        assert body["duration_ms"] is not None
        assert "total_tokens" not in body["data"], "telemetry keys must not leak into data"

    async def test_result_of_a_failed_job_is_409(self, api: Any) -> None:
        job = await _submit(api)

        from app.services.jobs import handlers as job_handlers

        original = job_handlers.HANDLERS["custom"]
        job_handlers.HANDLERS["custom"] = _always_fail
        try:
            async with _worker_for(api, job["id"]) as worker:
                assert await worker.drain(max_wait=20) is True
        finally:
            job_handlers.HANDLERS["custom"] = original

        response = await api.get(f"/api/v1/jobs/{job['id']}/result", headers=_h(USER))
        assert response.status_code == 409
        assert "blip" in response.text


def _always_fail(*args: Any, **kwargs: Any) -> Any:
    from app.services.jobs import JobHandlerError

    raise JobHandlerError("provider blip", code="blip")


class _worker_for:  # noqa: N801 - used as an async context manager in tests
    def __init__(self, client: Any, job_id: str) -> None:
        self._client = client
        self._job_id = job_id
        self._worker: JobWorker | None = None

    async def __aenter__(self) -> JobWorker:
        factory = self._client.app.state.job_session_factory
        self._worker = JobWorker(factory, name="fail-worker")
        await self._worker.start()
        return self._worker

    async def __aexit__(self, *exc: object) -> None:
        if self._worker is not None:
            await self._worker.stop()


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
class TestLogs:
    async def test_logs_are_empty_before_the_job_runs(self, api: Any) -> None:
        job = await _submit(api)
        response = await api.get(f"/api/v1/jobs/{job['id']}/logs", headers=_h(USER))
        assert response.status_code == 200
        assert response.json() == []

    async def test_logs_expose_duration_and_token_telemetry(
        self, api: Any, queue_engine: async_sessionmaker[AsyncSession]
    ) -> None:
        from app.core.metrics import TokenUsage
        from app.services.jobs import JobTelemetry, flush

        job = await _submit(api)
        async with queue_engine() as session:
            row = await session.get(BackgroundJob, uuid.UUID(job["id"]))
            assert row is not None
            telemetry = JobTelemetry(job_id=row.id, attempt=1)
            telemetry.record_tokens(TokenUsage(prompt_tokens=10, completion_tokens=5))
            telemetry.event("job.progress", "halfway", progress_pct=50)
            await flush(
                session,
                telemetry,
                job_type="custom",
                state=JobState.COMPLETED,
                max_entries=100,
            )
            await session.commit()

        response = await api.get(f"/api/v1/jobs/{job['id']}/logs", headers=_h(USER))
        entries = response.json()
        assert [entry["sequence"] for entry in entries] == list(range(len(entries)))

        terminal = [entry for entry in entries if entry["event"].startswith("job.")]
        assert terminal
        assert terminal[-1]["data"]["total_tokens"] == 15
        assert "duration_ms" in terminal[-1]["data"]
        assert any(entry["event"] == "job.progress" for entry in entries)

    async def test_after_cursor_returns_only_new_entries(
        self, api: Any, queue_engine: async_sessionmaker[AsyncSession]
    ) -> None:
        job = await _submit(api)
        async with queue_engine() as session:
            row = await session.get(BackgroundJob, uuid.UUID(job["id"]))
            assert row is not None
            for index in range(4):
                session.add(
                    JobLog(job_id=row.id, sequence=index, event="t", message=f"m{index}", data={})
                )
            await session.commit()

        all_logs = (await api.get(f"/api/v1/jobs/{job['id']}/logs", headers=_h(USER))).json()
        assert len(all_logs) == 4

        tail = (
            await api.get(f"/api/v1/jobs/{job['id']}/logs?after=2", headers=_h(USER))
        ).json()
        assert [entry["sequence"] for entry in tail] == [3]

    async def test_logs_of_another_users_job_are_404(self, api: Any) -> None:
        job = await _submit(api)
        response = await api.get(f"/api/v1/jobs/{job['id']}/logs", headers=_h(OTHER_USER))
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Cancel / delete
# ---------------------------------------------------------------------------
class TestCancel:
    async def test_queued_job_cancels_immediately(self, api: Any) -> None:
        job = await _submit(api)
        response = await api.post(f"/api/v1/jobs/{job['id']}/cancel", headers=_h(USER))
        assert response.status_code == 200
        assert response.json()["state"] == "cancelled"
        assert response.json()["finished_at"] is not None

    async def test_cancelling_twice_is_409(self, api: Any) -> None:
        job = await _submit(api)
        await api.post(f"/api/v1/jobs/{job['id']}/cancel", headers=_h(USER))
        again = await api.post(f"/api/v1/jobs/{job['id']}/cancel", headers=_h(USER))
        assert again.status_code == 409

    async def test_cancelling_another_users_job_is_404(self, api: Any) -> None:
        job = await _submit(api)
        response = await api.post(f"/api/v1/jobs/{job['id']}/cancel", headers=_h(OTHER_USER))
        assert response.status_code == 404

    async def test_cancelled_job_never_runs(
        self, api: Any, queue_engine: async_sessionmaker[AsyncSession]
    ) -> None:
        from app.services.jobs import handlers as job_handlers

        job = await _submit(api)
        await api.post(f"/api/v1/jobs/{job['id']}/cancel", headers=_h(USER))

        ran = {"n": 0}

        async def track(*args: Any, **kwargs: Any) -> dict[str, Any]:
            ran["n"] += 1
            return {}

        original = job_handlers.HANDLERS["custom"]
        job_handlers.HANDLERS["custom"] = track
        try:
            worker = JobWorker(queue_engine, name="w")
            await worker.start()
            try:
                await worker.drain(max_wait=10)
            finally:
                await worker.stop()
        finally:
            job_handlers.HANDLERS["custom"] = original

        assert ran["n"] == 0


class TestDelete:
    async def test_deleting_a_finished_job_removes_its_log(
        self, api: Any, queue_engine: async_sessionmaker[AsyncSession]
    ) -> None:
        from app.services.jobs import JobTelemetry

        job = await _submit(api)
        async with queue_engine() as session:
            row = await session.get(BackgroundJob, uuid.UUID(job["id"]))
            assert row is not None
            row.state = JobState.COMPLETED.value
            await JobTelemetry(job_id=row.id).flush_placeholder() if False else None
            session.add(
                JobLog(job_id=row.id, sequence=0, event="t", message="m", data={})
            )
            await session.commit()

        response = await api.delete(f"/api/v1/jobs/{job['id']}", headers=_h(USER))
        assert response.status_code == 204

        async with queue_engine() as session:
            assert await session.get(BackgroundJob, uuid.UUID(job["id"])) is None
            logs = await session.scalars(
                select(JobLog).where(JobLog.job_id == uuid.UUID(job["id"]))
            )
            assert logs.all() == []

    async def test_deleting_a_queued_job_is_409(
        self, api: Any, queue_engine: async_sessionmaker[AsyncSession]
    ) -> None:
        """Deleting live work would strand the worker mid-flight."""
        job = await _submit(api)
        response = await api.delete(f"/api/v1/jobs/{job['id']}", headers=_h(USER))
        assert response.status_code == 409

    async def test_deleting_another_users_job_is_404(self, api: Any) -> None:
        job = await _submit(api)
        response = await api.delete(f"/api/v1/jobs/{job['id']}", headers=_h(OTHER_USER))
        assert response.status_code == 404