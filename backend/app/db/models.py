"""ORM models for NexusFlow AI.

Uses SQLAlchemy 2.0 ``Mapped`` annotations. PostgreSQL-native types are applied
via ``with_variant`` so the same metadata can be exercised against SQLite in
the test suite.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
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

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<User {self.email}>"


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

    __table_args__ = (Index("ix_workflow_runs_status_created", "status", "created_at"),)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<WorkflowRun {self.model} {self.status}>"
