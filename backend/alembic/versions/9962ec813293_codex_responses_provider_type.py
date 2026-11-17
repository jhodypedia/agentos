"""codex responses provider type

Revision ID: 9962ec813293
Revises: e146a62bc18c
Create Date: 2026-11-15 11:32:28.092614

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "9962ec813293"
down_revision: str | None = "e146a62bc18c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_model_providers_providertype"
COLUMN = "model_providers.provider_type"


def _values() -> list[str]:
    from app.db.enums import ProviderType

    return [member.value for member in ProviderType]


def upgrade() -> None:
    values = ", ".join(f"'{value}'" for value in _values())
    name = op.f(CONSTRAINT)
    op.drop_constraint(name, "model_providers", type_="check")
    op.create_check_constraint(name, "model_providers", f"{COLUMN} IN ({values})")


def downgrade() -> None:
    values = ", ".join(f"'{value}'" for value in _values() if value != "codex_responses")
    name = op.f(CONSTRAINT)
    op.drop_constraint(name, "model_providers", type_="check")
    op.create_check_constraint(name, "model_providers", f"{COLUMN} IN ({values})")
