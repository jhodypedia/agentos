"""task attempt counters

Revision ID: e146a62bc18c
Revises: 9d6bea85789b
Create Date: 2026-11-15 10:36:02.692374
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e146a62bc18c'
down_revision: str | None = '9d6bea85789b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'tasks',
        sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    )
    op.add_column(
        'tasks',
        sa.Column('max_attempts', sa.Integer(), server_default=sa.text('1'), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('tasks', 'max_attempts')
    op.drop_column('tasks', 'attempts')
