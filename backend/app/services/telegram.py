"""Private-chat commands reuse the platform's persistent services and roles."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import PermissionDeniedError, UnprocessableError
from app.db.enums import ApprovalStatus, UserRole
from app.db.models import Agent, Approval, Competition, Finding, Task, User
from app.services import approvals, research, tasks
from app.services.audit import record_audit

HELP = """AgentOS commands:
/start, /help — show commands
/status — system and task status
/agents — available agents
/tasks — recent tasks; /tasks create <agent-slug> <objective>
/bounties — synced competitions
/scan — scanner availability (run scans through the admin research API)
/report <finding UUID> — local finding report
/cost — your recorded task costs
/approve [approval UUID], /reject <approval UUID>
/stop <task UUID> — cancel your task
Tasks are created pending; execution is started through the task API.
"""


def operator(user: User) -> None:
    if user.role == UserRole.VIEWER:
        raise PermissionDeniedError("This command requires operator privileges")


async def authenticated_user(db: AsyncSession, sender: int, chat_id: int, chat_type: str) -> User:
    if chat_type != "private" or chat_id != sender:
        raise PermissionDeniedError("Only private chats are supported")
    if sender not in get_settings().telegram_allowed_user_ids:
        raise PermissionDeniedError("Telegram account is not allowlisted")
    user = (
        await db.scalars(
            select(User).where(User.telegram_user_id == sender, User.is_active.is_(True))
        )
    ).first()
    if user is None:
        raise PermissionDeniedError("An administrator must link your Telegram account first")
    return user


async def command(db: AsyncSession, user: User, text: str) -> dict[str, Any]:
    parts = text.strip().split(maxsplit=2)
    cmd = parts[0].split("@")[0].lower() if parts else "/help"
    arg = " ".join(parts[1:])
    if cmd in {"/help", "/start"}:
        return {"text": HELP}
    if cmd == "/status":
        rows = (await db.execute(select(Task.status, func.count()).group_by(Task.status))).all()
        return {"text": "Database: connected\nTasks: " + ", ".join(f"{s}: {n}" for s, n in rows)}
    if cmd == "/agents":
        agents = await db.scalars(select(Agent).order_by(Agent.slug).limit(30))
        return {"text": "Agents:\n" + "\n".join(f"{a.slug}: {a.status.value}" for a in agents)}
    if cmd == "/bounties":
        items = await db.scalars(select(Competition).order_by(Competition.deadline_at).limit(10))
        return {
            "text": "Synced board:\n"
            + "\n".join(
                f"{c.external_id}: {c.status.value}, {c.reward_amount} {c.reward_currency}, "
                f"deadline {c.deadline_at}"
                for c in items
            )
        }
    if cmd == "/scan":
        inventory = await research.scanner_inventory()
        return {
            "text": "Scanners:\n"
            + "\n".join(
                f"{i['name']}: {'available' if i['available'] else 'unavailable'}"
                for i in inventory
            )
        }
    if cmd == "/cost":
        value = await db.scalar(
            select(func.coalesce(func.sum(Task.cost_usd), 0)).where(Task.created_by == user.id)
        )
        return {"text": f"Your recorded task cost: ${Decimal(str(value)):.6f}"}
    operator(user)
    if cmd == "/tasks":
        if arg.startswith("create "):
            create = arg.split(maxsplit=2)
            if len(create) != 3:
                raise UnprocessableError("Use /tasks create <agent-slug> <objective>")
            agent = (await db.scalars(select(Agent).where(Agent.slug == create[1]))).first()
            if agent is None:
                raise UnprocessableError("Agent not found")
            task = await tasks.create_task(
                db,
                title=create[2][:300],
                objective=create[2],
                assigned_agent_id=agent.id,
                created_by=user.id,
                meta={"telegram_chat_id": user.telegram_user_id},
                budget_usd=Decimal("1.00"),
            )
            await record_audit(
                db,
                action="telegram.task.created",
                actor_user_id=user.id,
                resource_type="task",
                resource_id=task.id,
            )
            return {"text": f"Task created: {task.id}\nStatus: {task.status.value}"}
        items = await db.scalars(
            select(Task)
            .where(Task.created_by == user.id)
            .order_by(Task.created_at.desc())
            .limit(10)
        )
        return {
            "text": "Your tasks:\n"
            + "\n".join(f"{t.id}: {t.status.value} — {t.title}" for t in items)
        }
    if cmd == "/stop":
        task = await tasks.get_task(db, UUID(arg))
        if task.created_by != user.id and user.role not in {UserRole.ADMIN, UserRole.OWNER}:
            raise PermissionDeniedError("Task belongs to another user")
        await tasks.cancel_task(db, task, message="Cancelled through Telegram")
        await approvals.cancel_pending_for_task(db, task_id=task.id)
        await record_audit(
            db,
            action="telegram.task.cancelled",
            actor_user_id=user.id,
            resource_type="task",
            resource_id=task.id,
        )
        return {"text": f"Task {task.id}: cancelled"}
    if cmd == "/report":
        finding = await db.get(Finding, UUID(arg))
        if finding is None:
            raise UnprocessableError("Finding not found")
        return {"text": (await research.report(db, finding))["markdown"][:15000]}
    if cmd in {"/approve", "/reject"}:
        if not arg and cmd == "/approve":
            query = select(Approval).where(
                Approval.status == ApprovalStatus.PENDING,
                (Approval.assigned_to_user_id == user.id) | Approval.assigned_to_user_id.is_(None),
            )
            approval = (await db.scalars(query.order_by(Approval.created_at).limit(1))).first()
            if approval is None:
                return {"text": "No pending approvals"}
            return approval_message(approval)
        approval_id = UUID(arg)
        approval = (
            await db.scalars(select(Approval).where(Approval.id == approval_id).with_for_update())
        ).first()
        if approval is None:
            raise UnprocessableError("Approval not found")
        if approval.assigned_to_user_id not in {None, user.id}:
            raise PermissionDeniedError("Approval is assigned to another user")
        decided = await approvals.decide(
            db,
            approval_id=approval_id,
            approved=cmd == "/approve",
            decided_by_user_id=user.id,
            note="Decided through Telegram",
        )
        await record_audit(
            db,
            action="telegram.approval.decided",
            actor_user_id=user.id,
            resource_type="approval",
            resource_id=approval_id,
        )
        return {"text": f"Approval {approval_id}: {decided.status.value}"}
    return {"text": HELP}


def approval_message(approval: Approval) -> dict[str, Any]:
    return {
        "text": f"Approval {approval.id}\n{approval.title}\n{approval.description}\n"
        f"Action: {approval.action_type.value}\nExpires: {approval.expires_at}\n"
        f"Payload: {json.dumps(approval.payload, default=str)[:1000]}",
        "keyboard": {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data": f"approve:{approval.id}"},
                    {"text": "Reject", "callback_data": f"reject:{approval.id}"},
                ]
            ]
        },
    }
