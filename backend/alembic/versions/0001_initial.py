"""Initial schema: users, workflows, workflow_runs.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: JSONB on PostgreSQL, plain JSON elsewhere (matches app/db/models.py).
JSONVariant = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=True),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_superuser", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        # The UNIQUE constraint is already indexed; a separate index would be
        # redundant.
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    op.create_table(
        "workflows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("definition", JSONVariant, nullable=False),
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
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_workflows_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflows")),
    )
    op.create_index("ix_workflows_owner_status", "workflows", ["owner_id", "status"], unique=False)

    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name=op.f("fk_workflow_runs_workflow_id_workflows"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_runs")),
    )
    op.create_index(
        "ix_workflow_runs_status_created",
        "workflow_runs",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_runs_workflow_id"), "workflow_runs", ["workflow_id"], unique=False
    )
    op.create_index(op.f("ix_workflow_runs_status"), "workflow_runs", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_workflow_runs_status"), table_name="workflow_runs")
    op.drop_index(op.f("ix_workflow_runs_workflow_id"), table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_status_created", table_name="workflow_runs")
    op.drop_table("workflow_runs")

    op.drop_index("ix_workflows_owner_status", table_name="workflows")
    op.drop_table("workflows")

    op.drop_table("users")
