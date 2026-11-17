"""Editable platform settings.

Only keys the operator CLI seeds are writable, so this route cannot be used to
create arbitrary rows. Secrets stay in environment variables and are reported
as configured/not configured, never echoed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import AdminPrincipal, DbSession
from app.core.config import get_settings
from app.core.exceptions import NotFoundError
from app.db.models import SystemSetting
from app.services.audit import record_audit

router = APIRouter(prefix="/settings", tags=["settings"])

EDITABLE_KEYS = frozenset(
    {
        "platform.registration_enabled",
        "security.require_telegram_allowlist",
        "security.approvals_required",
        "execution.sandbox_enabled",
        "execution.workspace_quota_mb",
        "orchestrator.max_parallel_tasks",
        "orchestrator.default_task_timeout_seconds",
        "orchestrator.default_task_budget_usd",
        "memory.retention_days",
        "ui.theme",
    }
)


class SettingPublic(BaseModel):
    key: str
    value: dict[str, Any]
    description: str
    is_secret: bool
    editable: bool
    updated_at: datetime


class SettingUpdate(BaseModel):
    value: dict[str, Any] = Field(min_length=0)


class SettingList(BaseModel):
    items: list[SettingPublic]
    environment: dict[str, Any]


def _public(row: SystemSetting) -> SettingPublic:
    return SettingPublic(
        key=row.key,
        value=row.value or {},
        description=row.description,
        is_secret=row.is_secret,
        editable=row.key in EDITABLE_KEYS and not row.is_secret,
        updated_at=row.updated_at,
    )


def _environment() -> dict[str, Any]:
    cfg = get_settings()
    return {
        "environment": cfg.environment,
        "app_version": cfg.app_version,
        "sandbox_enabled": cfg.sandbox_enabled,
        "sandbox_image": cfg.sandbox_image,
        "sandbox_allow_host_execution": cfg.sandbox_allow_host_execution,
        "workspace_root": str(cfg.workspace_root),
        "skills_root": str(cfg.skills_root),
        "telegram_bot_configured": cfg.telegram_bot_token is not None,
        "telegram_allowed_user_count": len(cfg.telegram_allowed_user_ids),
        "telegram_webhook_configured": cfg.telegram_webhook_secret is not None,
        "makefun_configured": cfg.makefun_api_key is not None,
        "coding_engine": cfg.coding_engine,
        "memory_embedding_model": cfg.memory_embedding_model,
    }


@router.get("", response_model=SettingList, summary="List platform settings")
async def list_settings(principal: AdminPrincipal, db: DbSession) -> SettingList:
    rows = list((await db.scalars(select(SystemSetting).order_by(SystemSetting.key))).all())
    return SettingList(items=[_public(row) for row in rows], environment=_environment())


@router.patch("/{key:path}", response_model=SettingPublic, summary="Update a setting value")
async def update_setting(
    key: str,
    payload: SettingUpdate,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> SettingPublic:
    row = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Unknown setting")
    if row.key not in EDITABLE_KEYS or row.is_secret:
        raise NotFoundError("This setting is not editable")
    previous = dict(row.value or {})
    row.value = payload.value
    row.updated_by = principal.user.id
    await record_audit(
        db,
        action="setting.updated",
        resource_type="setting",
        resource_id=row.key,
        actor_user_id=principal.user.id,
        request=request,
        meta={"previous": previous, "value": payload.value},
    )
    await db.commit()
    await db.refresh(row)
    return _public(row)
