"""ORM models for NexusFlow AI.

Uses SQLAlchemy 2.0 ``Mapped`` annotations. PostgreSQL-native types are applied
via ``with_variant`` so the same metadata can be exercised against SQLite in
the test suite.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: Portable JSON column — JSONB on PostgreSQL, JSON elsewhere.
JSONVariant = JSON().with_variant(JSONB(), "postgresql")


class UserRole(enum.StrEnum):
    MEMBER = "member"
    ADMIN = "admin"


class WorkflowStatus(enum.StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class RunStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DocumentStatus(enum.StrEnum):
    """Lifecycle of an uploaded document and its extraction pipeline."""

    PENDING = "pending"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    EMBEDDING = "embedding"
    READY = "ready"
    FAILED = "failed"


class JobState(enum.StrEnum):
    """States of a simulated Nebius Serverless Job."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Whether no further transition is possible from this state."""
        return self in _TERMINAL_JOB_STATES


class JobType(enum.StrEnum):
    """Workload kinds a background job can carry."""

    DOCUMENT_EXTRACTION = "document_extraction"
    DOCUMENT_EMBEDDING = "document_embedding"
    WORKFLOW_RUN = "workflow_run"
    EMBEDDING = "embedding"
    CUSTOM = "custom"


class StepKind(enum.StrEnum):
    """Node types recorded in a run's step history."""

    LLM = "llm"
    RETRIEVAL = "retrieval"
    TOOL = "tool"
    TRANSFORM = "transform"
    ROUTER = "router"
    HUMAN = "human"


#: States from which a job can never move again. ``failed`` is *not* terminal:
#: it is requeued while retry attempts remain, which is what lets a transient
#: provider error recover without operator intervention.
_TERMINAL_JOB_STATES: frozenset[JobState] = frozenset({JobState.COMPLETED, JobState.CANCELLED})

#: Legal state transitions for a background job. ``completed`` is terminal;
#: ``failed`` may be requeued while retry attempts remain, which is what lets a
#: transient provider error recover without operator intervention.
#: ``cancelled`` is reachable from both live states so a queued or in-flight job
#: can be stopped, but a job that already finished cannot be cancelled.
JOB_STATE_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.PROCESSING, JobState.FAILED, JobState.CANCELLED}),
    JobState.PROCESSING: frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}),
    JobState.COMPLETED: frozenset(),
    JobState.CANCELLED: frozenset(),
    JobState.FAILED: frozenset({JobState.QUEUED}),
}


def can_transition(current: JobState | str, target: JobState | str) -> bool:
    """Return True when a job may move from ``current`` to ``target``."""
    try:
        source = JobState(current)
        destination = JobState(target)
    except ValueError:
        return False
    return destination in JOB_STATE_TRANSITIONS[source]


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    # `unique=True` emits a UNIQUE constraint, which is already backed by an
    # index — adding `index=True` here would create a redundant second one.
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default=UserRole.MEMBER.value, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workflows: Mapped[list[Workflow]] = relationship(
        back_populates="owner", cascade="all, delete-orphan", lazy="selectin"
    )
    documents: Mapped[list[Document]] = relationship(
        back_populates="owner", cascade="all, delete-orphan", lazy="selectin"
    )
    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<User {self.email}>"


class UserSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A refreshable login session.

    Only the *hash* of the refresh token is persisted, so a database leak cannot
    be replayed against the auth endpoints. ``token_family_id`` groups the
    tokens minted by one login so that reusing a rotated token can revoke the
    whole chain.
    """

    __tablename__ = "user_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    token_family_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), default=uuid.uuid4, nullable=False
    )
    user_agent: Mapped[str | None] = mapped_column(String(400))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="sessions", lazy="selectin")

    __table_args__ = (Index("ix_user_sessions_user_active", "user_id", "revoked_at"),)

    @property
    def is_revoked(self) -> bool:
        """True once the session has been explicitly invalidated."""
        return self.revoked_at is not None

    def is_expired(self, *, now: datetime) -> bool:
        """Report whether ``now`` is at or past the expiry instant."""
        expires_at = self.expires_at
        # SQLite round-trips naive datetimes; normalise before comparing.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=now.tzinfo)
        return expires_at <= now

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<UserSession user={self.user_id} revoked={self.is_revoked}>"


class Workflow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflows"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), default=WorkflowStatus.DRAFT.value, nullable=False
    )
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict, nullable=False)

    owner: Mapped[User] = relationship(back_populates="workflows", lazy="selectin")
    runs: Mapped[list[WorkflowRun]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_workflows_owner_status", "owner_id", "status"),)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Workflow {self.name}>"


class WorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Execution ledger: one row per model invocation, with token accounting."""

    __tablename__ = "workflow_runs"

    workflow_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=RunStatus.QUEUED.value, nullable=False, index=True
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workflow: Mapped[Workflow | None] = relationship(back_populates="runs")
    steps: Mapped[list[WorkflowStep]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_workflow_runs_status_created", "status", "created_at"),)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<WorkflowRun {self.model} {self.status}>"


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An uploaded source file plus the state of its ingestion pipeline.

    ``storage_path`` points at the original bytes on disk (or in object
    storage); everything derived from it — extracted text, chunks, embeddings —
    lives in :class:`DocumentChunk` rows so the extraction step can be retried
    without re-reading the source.
    """

    __tablename__ = "documents"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(400), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(200))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    page_count: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[str] = mapped_column(
        String(32), default=DocumentStatus.PENDING.value, nullable=False, index=True
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONVariant, default=dict, nullable=False
    )
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)

    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    owner: Mapped[User] = relationship(back_populates="documents", lazy="selectin")
    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_documents_owner_status", "owner_id", "status"),)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Document {self.filename} {self.status}>"


class DocumentChunk(UUIDPrimaryKeyMixin, Base):
    """One extracted slice of a document, with its optional embedding vector."""

    __tablename__ = "document_chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Plain JSON rather than a native vector type, so the same schema works on
    #: SQLite in tests and on PostgreSQL in production.
    embedding: Mapped[list[float] | None] = mapped_column(JSONVariant)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONVariant, default=dict, nullable=False
    )

    document: Mapped[Document] = relationship(back_populates="chunks", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_document_chunks_document_ordinal"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<DocumentChunk doc={self.document_id} ordinal={self.ordinal}>"


class WorkflowStep(Base):
    """One node of a run's step history.

    Steps are appended as the graph executes, so a partially failed run still
    shows which node died and how many tokens the earlier nodes burned.
    """

    __tablename__ = "workflow_steps"

    id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, primary_key=True, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default=StepKind.LLM.value, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=RunStatus.QUEUED.value, nullable=False, index=True
    )
    model: Mapped[str | None] = mapped_column(String(200))

    input: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    error: Mapped[str | None] = mapped_column(Text)

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[WorkflowRun] = relationship(back_populates="steps", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("run_id", "step_index", name="uq_workflow_steps_run_index"),
        Index("ix_workflow_steps_run_started", "run_id", "started_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<WorkflowStep {self.step_index}:{self.name} {self.status}>"


class BackgroundJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """State record for a simulated Nebius Serverless Job.

    NexusFlow owns dispatch, so the row doubles as both the queue entry and the
    audit trail: ``state`` moves ``queued → processing → completed | failed``,
    and :data:`JOB_STATE_TRANSITIONS` is the single source of truth for legal
    moves.
    """

    __tablename__ = "background_jobs"

    job_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    state: Mapped[str] = mapped_column(
        String(32), default=JobState.QUEUED.value, nullable=False, index=True
    )

    payload: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)

    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    submitted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    #: Identifier the simulated Nebius worker echoes back, mirroring the way a
    #: real Serverless Job ID is used to poll remote status.
    nebius_job_id: Mapped[str | None] = mapped_column(String(128), unique=True)

    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(128), index=True)

    progress_pct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))

    #: Set by the cancel endpoint. An in-flight job checks this at its next
    #: checkpoint and unwinds, so cancellation does not require killing the
    #: worker. A queued job is cancelled without ever being claimed.
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )

    # A freshly enqueued job is immediately visible to the dispatcher; a retry
    # backdates this to schedule the next attempt.
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=UTC),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    document: Mapped[Document | None] = relationship(lazy="selectin")
    submitted_by: Mapped[User | None] = relationship(lazy="selectin")

    __table_args__ = (
        Index("ix_background_jobs_dispatch", "state", "priority", "created_at"),
        Index("ix_background_jobs_state_available", "state", "available_at"),
    )

    def can_transition_to(self, target: JobState | str) -> bool:
        """Return True when this job may legally move to ``target``."""
        return can_transition(JobState(self.state), target)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<BackgroundJob {self.job_type} {self.state}>"


class JobLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One telemetry line emitted while a background job runs.

    Worker progress is otherwise invisible once the submitting HTTP request has
    returned, so each attempt appends here: state changes, durations, token
    counts, and the reason a job failed. Rows cascade with their job, which
    keeps a deleted job's log from becoming orphaned telemetry nobody can
    attribute.
    """

    __tablename__ = "job_logs"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("background_jobs.id", ondelete="CASCADE"), nullable=False
    )
    #: Zero-based position within the job, so a client can detect gaps.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    #: Free-form structured detail: token counts, durations, retry numbers.
    data: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict, nullable=False)
    #: Wall-clock offset from the job's first log line, in milliseconds.
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("job_id", "sequence", name="uq_job_logs_job_id_sequence"),
        Index("ix_job_logs_job_id_sequence", "job_id", "sequence"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<JobLog {self.job_id} #{self.sequence} {self.event}>"
