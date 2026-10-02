"""Background job telemetry logs and cancellation flag.

Revision ID: 0003_job_logs
Revises: 0002_documents_jobs
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_job_logs"
down_revision: str | None = "0002_documents_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: JSONB on PostgreSQL, plain JSON elsewhere (matches app/db/models.py).
JSONVariant = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # An in-flight job checks this flag at its next checkpoint, so cancelling
    # does not require killing the worker mid-extraction. Indexed because the
    # dispatcher polls for cancelled-but-still-claimed jobs on every sweep.
    op.add_column(
        "background_jobs",
        sa.Column(
            "cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.create_index(
        op.f("ix_background_jobs_cancel_requested"),
        "background_jobs",
        ["cancel_requested"],
        unique=False,
    )

    op.create_table(
        "job_logs",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("data", JSONVariant, nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Telemetry is meaningless without its job, so it dies with the job
        # rather than becoming an unattributable orphan.
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["background_jobs.id"],
            name=op.f("fk_job_logs_job_id_background_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_logs")),
        # One line per position: a client reading the log can prove nothing was
        # silently dropped, and concurrent appends cannot collide.
        sa.UniqueConstraint("job_id", "sequence", name="uq_job_logs_job_id_sequence"),
    )
    op.create_index(
        "ix_job_logs_job_id_sequence", "job_logs", ["job_id", "sequence"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_job_logs_job_id_sequence", table_name="job_logs")
    op.drop_table("job_logs")
    op.drop_index(op.f("ix_background_jobs_cancel_requested"), table_name="background_jobs")
    op.drop_column("background_jobs", "cancel_requested")