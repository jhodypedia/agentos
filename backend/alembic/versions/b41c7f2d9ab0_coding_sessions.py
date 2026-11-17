"""coding sessions

Revision ID: b41c7f2d9ab0
Revises: df885b5d878f
Create Date: 2026-11-16 09:12:04.551003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b41c7f2d9ab0"
down_revision: str | None = "df885b5d878f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coding_sessions",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("engine", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column("session_id", sa.String(length=200), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="running",
            nullable=False,
        ),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("transcript_path", sa.String(length=1000), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("changes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("commands", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'timed_out')",
            name=op.f("ck_coding_sessions_status"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name=op.f("fk_coding_sessions_agent_id_agents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_coding_sessions_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_coding_sessions_requested_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_coding_sessions_task_id_tasks"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coding_sessions")),
    )
    op.create_index(
        "ix_coding_sessions_project_created",
        "coding_sessions",
        ["project_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_coding_sessions_task", "coding_sessions", ["task_id"], unique=False
    )
    op.create_index(
        op.f("ix_coding_sessions_session_id"),
        "coding_sessions",
        ["session_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_coding_sessions_session_id"), table_name="coding_sessions")
    op.drop_index("ix_coding_sessions_task", table_name="coding_sessions")
    op.drop_index("ix_coding_sessions_project_created", table_name="coding_sessions")
    op.drop_table("coding_sessions")
