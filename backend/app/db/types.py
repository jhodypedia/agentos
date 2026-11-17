"""Portable SQLAlchemy column types."""

from __future__ import annotations

from enum import Enum as PyEnum

from sqlalchemy import Enum as SAEnum


def sa_enum(
    enum_cls: type[PyEnum],
    *,
    name: str | None = None,
    length: int = 32,
) -> SAEnum:
    """Return a VARCHAR-backed enum with a CHECK constraint.

    Native PostgreSQL enums are deliberately avoided so that adding values does
    not require a type rewrite during a deployment.
    """
    return SAEnum(
        enum_cls,
        name=name or enum_cls.__name__.lower(),
        native_enum=False,
        length=length,
        create_constraint=True,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )
