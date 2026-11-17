"""Notification delivery from committed task events and pending approvals."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.enums import ApprovalStatus
from app.db.models import Approval, Task, User
from app.services.telegram import approval_message
from app.telegram.client import TelegramClient

logger = structlog.get_logger("agentos.telegram.notifications")


async def deliver(factory: async_sessionmaker[AsyncSession], settings: Settings) -> int:
    if not settings.telegram_bot_token or not settings.telegram_notifications_enabled:
        return 0
    sent = 0
    transport = TelegramClient(settings.telegram_bot_token.get_secret_value())
    async with factory() as db:
        users = list(
            (
                await db.scalars(
                    select(User).where(
                        User.is_active.is_(True),
                        User.telegram_user_id.in_(settings.telegram_allowed_user_ids),
                    )
                )
            ).all()
        )
        for user in users:
            if user.telegram_user_id is None:
                continue
            tasks = list(
                (
                    await db.scalars(
                        select(Task)
                        .where(Task.created_by == user.id, Task.meta.has_key("telegram_chat_id"))
                        .order_by(Task.created_at.desc())
                        .limit(20)
                    )
                ).all()
            )
            for task in tasks:
                key = f"tg:{user.id}:{task.status.value}"
                if (task.meta or {}).get(key):
                    continue
                await transport.send(
                    user.telegram_user_id,
                    f"Task {task.id}\n{task.title}\nStatus: {task.status.value}\n"
                    f"Tokens: {task.used_tokens}\nCost: ${task.cost_usd}\n"
                    f"{task.error or ''}",
                )
                task.meta = {**task.meta, key: True}
                await db.commit()
                sent += 1
            if user.role.value == "viewer":
                continue
            approvals = list(
                (
                    await db.scalars(
                        select(Approval)
                        .where(
                            Approval.status == ApprovalStatus.PENDING,
                            (Approval.assigned_to_user_id == user.id)
                            | Approval.assigned_to_user_id.is_(None),
                        )
                        .limit(20)
                    )
                ).all()
            )
            for approval in approvals:
                key = f"telegram_notified:{user.id}"
                if (approval.payload or {}).get(key):
                    continue
                message: dict[str, Any] = approval_message(approval)
                await transport.send(user.telegram_user_id, message["text"], message["keyboard"])
                approval.payload = {**approval.payload, key: True}
                await db.commit()
                sent += 1
    return sent
