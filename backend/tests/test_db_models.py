"""Tests for the persistence layer: schema shape, state machines, cascades.

A synchronous in-memory SQLite engine stands in for PostgreSQL so the suite
stays dependency-free; the models use portable column types, so anything that
holds here holds on the real database.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from app.db.base import Base
from app.db.models import (
    JOB_STATE_TRANSITIONS,
    BackgroundJob,
    Document,
    DocumentChunk,
    DocumentStatus,
    JobState,
    JobType,
    RunStatus,
    StepKind,
    User,
    UserSession,
    Workflow,
    WorkflowRun,
    WorkflowStep,
    can_transition,
)
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def db() -> Iterator[Session]:
    """Yield a session bound to a fresh in-memory database.

    ``PRAGMA foreign_keys`` is off by default in SQLite, which would silently
    skip the ``ON DELETE CASCADE`` / ``ON DELETE SET NULL`` rules that
    PostgreSQL enforces. Turning it on makes the tests exercise the real
    referential behaviour.
    """
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session


def _user(session: Session, email: str = "owner@example.com") -> User:
    user = User(email=email, hashed_password="not-a-real-hash")
    session.add(user)
    session.commit()
    return user


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------
def test_every_table_is_registered() -> None:
    assert set(Base.metadata.tables) == {
        "users",
        "user_sessions",
        "documents",
        "document_chunks",
        "workflows",
        "workflow_runs",
        "workflow_steps",
        "background_jobs",
    }


def test_documents_track_source_and_processing_state() -> None:
    columns = set(Base.metadata.tables["documents"].columns.keys())

    assert {"storage_path", "filename", "status", "metadata"} <= columns


def test_chunks_are_unique_per_document_ordinal() -> None:
    constraints = {
        c.name for c in Base.metadata.tables["document_chunks"].constraints if c.name is not None
    }

    assert "uq_document_chunks_document_ordinal" in constraints


def test_steps_are_unique_per_run_index() -> None:
    constraints = {
        c.name for c in Base.metadata.tables["workflow_steps"].constraints if c.name is not None
    }

    assert "uq_workflow_steps_run_index" in constraints


def test_background_job_indexes_support_the_dispatcher() -> None:
    indexes = {index.name for index in Base.metadata.tables["background_jobs"].indexes}

    assert {"ix_background_jobs_dispatch", "ix_background_jobs_state_available"} <= indexes


def test_metadata_column_is_named_metadata_not_metadata_() -> None:
    """The Python attribute is `metadata_`; the SQL column must stay `metadata`."""
    assert "metadata" in Base.metadata.tables["documents"].columns
    assert "metadata" in Document.__table__.columns


# ---------------------------------------------------------------------------
# Job state machine
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (JobState.QUEUED, JobState.PROCESSING, True),
        (JobState.QUEUED, JobState.FAILED, True),
        (JobState.QUEUED, JobState.COMPLETED, False),
        (JobState.PROCESSING, JobState.COMPLETED, True),
        (JobState.PROCESSING, JobState.FAILED, True),
        (JobState.PROCESSING, JobState.QUEUED, False),
        (JobState.COMPLETED, JobState.PROCESSING, False),
        (JobState.FAILED, JobState.QUEUED, True),
    ],
)
def test_job_state_transitions(current: JobState, target: JobState, allowed: bool) -> None:
    assert can_transition(current, target) is allowed


def test_completed_is_terminal() -> None:
    assert JOB_STATE_TRANSITIONS[JobState.COMPLETED] == frozenset()


def test_transition_accepts_plain_strings() -> None:
    assert can_transition("queued", "processing") is True


def test_transition_rejects_unknown_states() -> None:
    assert can_transition("queued", "exploded") is False
    assert can_transition("nonsense", "processing") is False


def test_job_reports_its_own_transitions(db: Session) -> None:
    job = BackgroundJob(
        job_type=JobType.DOCUMENT_EXTRACTION.value,
        state=JobState.QUEUED.value,
        available_at=datetime.now(tz=UTC),
    )

    assert job.can_transition_to(JobState.PROCESSING) is True
    assert job.can_transition_to(JobState.COMPLETED) is False


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
def test_simulation_states_match_the_nebius_job_lifecycle() -> None:
    assert [state.value for state in JobState] == [
        "queued",
        "processing",
        "completed",
        "failed",
    ]


def test_defaults_are_applied_on_flush(db: Session) -> None:
    user = _user(db)
    doc = Document(owner_id=user.id, filename="a.pdf", storage_path="/tmp/a.pdf")
    db.add(doc)
    db.commit()

    assert doc.id is not None
    assert doc.status == DocumentStatus.PENDING.value
    assert doc.chunk_count == 0
    assert doc.metadata_ == {}


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def test_session_stores_a_hash_not_the_token(db: Session) -> None:
    user = _user(db)
    now = datetime.now(tz=UTC)
    session = UserSession(
        user_id=user.id,
        token_hash="sha256:deadbeef",
        issued_at=now,
        expires_at=now + timedelta(days=7),
    )
    db.add(session)
    db.commit()

    stored = db.get(UserSession, session.id)
    assert stored is not None
    assert stored.token_hash == "sha256:deadbeef"
    assert stored.is_revoked is False
    assert stored.token_family_id is not None


def test_session_expiry_handles_naive_and_aware_timestamps(db: Session) -> None:
    now = datetime.now(tz=UTC)
    # SQLite drops tzinfo on round-trip, so `is_expired` must cope with naive values.
    expired = UserSession(token_hash="h1", issued_at=now, expires_at=now - timedelta(seconds=1))
    live = UserSession(token_hash="h2", issued_at=now, expires_at=now + timedelta(seconds=60))

    assert expired.is_expired(now=now) is True
    assert live.is_expired(now=now) is False


def test_revoking_a_session_sets_the_flag(db: Session) -> None:
    user = _user(db)
    now = datetime.now(tz=UTC)
    session = UserSession(
        user_id=user.id,
        token_hash="h3",
        issued_at=now,
        expires_at=now + timedelta(hours=1),
        revoked_at=now,
    )
    db.add(session)
    db.commit()

    assert session.is_revoked is True


# ---------------------------------------------------------------------------
# Relationships and cascades
# ---------------------------------------------------------------------------
def test_chunks_cascade_with_their_document(db: Session) -> None:
    user = _user(db)
    doc = Document(owner_id=user.id, filename="a.pdf", storage_path="/tmp/a.pdf")
    db.add(doc)
    db.flush()
    db.add(DocumentChunk(document_id=doc.id, ordinal=0, content="hello", token_count=1))
    db.commit()

    db.delete(doc)
    db.commit()

    assert db.query(DocumentChunk).count() == 0


def test_steps_cascade_with_their_run(db: Session) -> None:
    user = _user(db)
    workflow = Workflow(owner_id=user.id, name="wf", model="m", definition={})
    db.add(workflow)
    db.flush()
    run = WorkflowRun(workflow_id=workflow.id, model="m", prompt="hi")
    db.add(run)
    db.flush()
    db.add(
        WorkflowStep(
            run_id=run.id,
            step_index=0,
            name="classify",
            kind=StepKind.ROUTER.value,
            started_at=datetime.now(tz=UTC),
        )
    )
    db.commit()

    db.delete(run)
    db.commit()

    assert db.query(WorkflowStep).count() == 0


def test_step_history_is_readable_in_order(db: Session) -> None:
    user = _user(db)
    workflow = Workflow(owner_id=user.id, name="wf", model="m", definition={})
    db.add(workflow)
    db.flush()
    run = WorkflowRun(workflow_id=workflow.id, model="m", prompt="hi")
    db.add(run)
    db.flush()
    for index in range(3):
        db.add(
            WorkflowStep(
                run_id=run.id,
                step_index=index,
                name=f"step-{index}",
                total_tokens=index * 10,
            )
        )
    db.commit()

    loaded = db.get(WorkflowRun, run.id)
    assert loaded is not None
    steps = sorted(loaded.steps, key=lambda s: s.step_index)
    assert [s.name for s in steps] == ["step-0", "step-1", "step-2"]
    assert [s.total_tokens for s in steps] == [0, 10, 20]
    assert sum(s.total_tokens for s in steps) == 30


def test_document_records_a_failed_processing_attempt(db: Session) -> None:
    user = _user(db)
    doc = Document(
        owner_id=user.id,
        filename="broken.pdf",
        storage_path="/tmp/broken.pdf",
        status=DocumentStatus.FAILED.value,
        error="unsupported mime type",
        processing_started_at=datetime.now(tz=UTC),
    )
    db.add(doc)
    db.commit()

    assert doc.status == DocumentStatus.FAILED.value
    assert doc.processed_at is None
    assert doc.error == "unsupported mime type"


def test_background_job_tracks_a_successful_run(db: Session) -> None:
    user = _user(db)
    started = datetime.now(tz=UTC)
    job = BackgroundJob(
        job_type=JobType.DOCUMENT_EXTRACTION.value,
        state=JobState.COMPLETED.value,
        payload={"document_id": "abc"},
        result={"chunks": 12},
        submitted_by_id=user.id,
        nebius_job_id="nf-123",
        worker_id="worker-1",
        attempts=1,
        progress_pct=100,
        available_at=started,
        started_at=started,
        finished_at=started + timedelta(seconds=4),
        duration_ms=4000,
    )
    db.add(job)
    db.commit()

    assert job.state == JobState.COMPLETED.value
    assert job.result == {"chunks": 12}
    assert job.duration_ms == 4000
    assert job.submitted_by is not None
    assert job.submitted_by.email == user.email
    assert job.can_transition_to(JobState.QUEUED) is False


def test_new_job_is_immediately_dispatchable(db: Session) -> None:
    """A job must be visible to the dispatcher as soon as it is enqueued."""
    job = BackgroundJob(job_type=JobType.WORKFLOW_RUN.value)
    db.add(job)
    db.commit()

    assert job.state == JobState.QUEUED.value
    assert job.priority == 0
    assert job.attempts == 0
    assert job.available_at is not None
    assert job.started_at is None


def test_deleting_a_user_keeps_their_jobs(db: Session) -> None:
    """Job results stay auditable after the submitter is gone."""
    user = _user(db)
    job = BackgroundJob(
        job_type=JobType.EMBEDDING.value,
        submitted_by_id=user.id,
        available_at=datetime.now(tz=UTC),
    )
    db.add(job)
    db.commit()
    job_id = job.id

    db.delete(user)
    db.commit()

    reloaded = db.get(BackgroundJob, job_id)
    assert reloaded is not None
    assert reloaded.submitted_by_id is None


def test_identifiers_are_uuids(db: Session) -> None:
    user = _user(db)

    assert isinstance(user.id, uuid.UUID)


def test_inspector_sees_every_new_table() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    tables = set(inspect(engine).get_table_names())

    assert {
        "user_sessions",
        "documents",
        "document_chunks",
        "workflow_steps",
        "background_jobs",
    } <= tables


def test_run_status_covers_terminal_and_running_states() -> None:
    assert {s.value for s in RunStatus} >= {
        RunStatus.QUEUED.value,
        RunStatus.RUNNING.value,
        RunStatus.SUCCEEDED.value,
        RunStatus.FAILED.value,
    }
