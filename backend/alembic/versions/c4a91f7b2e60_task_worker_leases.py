"""task worker leases

Revision ID: c4a91f7b2e60
Revises: e80e55457f5e
Create Date: 2026-11-17 09:14:22.4183

A worker claims work by taking a time-boxed lease on a task. Without one a
crashed worker leaves its task in ``running`` indefinitely, because nothing
else can tell "still working" from "gone". ``claimed_by`` records the owner for
diagnosis; ``lease_expires_at`` is what actually decides eligibility.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c4a91f7b2e60'
down_revision: str | None = 'e80e55457f5e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('claimed_by', sa.String(length=200), nullable=True))
    op.add_column(
        'tasks', sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        'ix_tasks_claimable',
        'tasks',
        ['status', 'scheduled_at', 'lease_expires_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_tasks_claimable', table_name='tasks')
    op.drop_column('tasks', 'lease_expires_at')
    op.drop_column('tasks', 'claimed_by')
