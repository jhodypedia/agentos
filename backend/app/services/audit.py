"""Audit logging helper.

Audit records are added to the caller's session and committed with the
surrounding business transaction so that actions and their audit trail are
atomic.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    if request.client is not None:
        return request.client.host[:64]
    return None


async def record_audit(
    db: AsyncSession,
    *,
    action: str,
    resource_type: str,
    resource_id: str | UUID | None = None,
    actor_user_id: UUID | None = None,
    actor_agent_id: UUID | None = None,
    request: Request | None = None,
    success: bool = True,
    meta: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_user_id=actor_user_id,
        actor_agent_id=actor_agent_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        ip_address=_client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:400] if request else None,
        success=success,
        meta=meta or {},
    )
    db.add(entry)
    return entry
