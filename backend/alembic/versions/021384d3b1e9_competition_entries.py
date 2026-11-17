"""competition entries

Revision ID: 021384d3b1e9
Revises: b41c7f2d9ab0
Create Date: 2026-11-16 01:54:23.382505
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "021384d3b1e9"
down_revision: str | None = "b41c7f2d9ab0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENTRY_STATUSES = (
    "draft",
    "prepared",
    "awaiting_approval",
    "submitted",
    "rejected",
    "validating",
    "graded",
    "placed",
    "won",
    "failed",
    "withdrawn",
)


def upgrade() -> None:
    op.create_table(
        "competition_entries",
        sa.Column("competition_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("submission_id", sa.String(length=120), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("repo_url", sa.String(length=1000), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("transcript_paths", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("score", sa.String(length=64), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("prize_usd", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
            "status IN ({})".format(", ".join(f"'{value}'" for value in _ENTRY_STATUSES)),
            name=op.f("ck_competition_entries_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_competition_entries")),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approvals.id"],
            name=op.f("fk_competition_entries_approval_id_approvals"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["competition_id"],
            ["competitions.id"],
            name=op.f("fk_competition_entries_competition_id_competitions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_competition_entries_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_competition_entries_project_id_projects"),
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_competition_entries_competition",
        "competition_entries",
        ["competition_id"],
        unique=False,
    )
    op.create_index(
        "ix_competition_entries_status_created",
        "competition_entries",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_competition_entries_submission",
        "competition_entries",
        ["submission_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_competition_entries_submission", table_name="competition_entries")
    op.drop_index("ix_competition_entries_status_created", table_name="competition_entries")
    op.drop_index("ix_competition_entries_competition", table_name="competition_entries")
    op.drop_table("competition_entries")
