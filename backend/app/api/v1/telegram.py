from __future__ import annotations

import hmac
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.api.deps import AdminPrincipal, DbSession
from app.core.exceptions import AppError, PermissionDeniedError, UnprocessableError
from app.db.models import TelegramUpdate, User
from app.memory.sensitivity import redact
from app.schemas.telegram import TelegramLink, TelegramUpdatePayload, WebhookSetup
from app.services import telegram
from app.services.audit import record_audit
from app.telegram.client import TelegramClient

router = APIRouter(prefix="/telegram", tags=["telegram"])


def client(request: Request) -> TelegramClient:
    token = request.app.state.settings.telegram_bot_token
    if not token:
        raise UnprocessableError("TELEGRAM_BOT_TOKEN is not configured")
    return TelegramClient(token.get_secret_value())


@router.post("/link")
async def link(
    payload: TelegramLink, principal: AdminPrincipal, db: DbSession, request: Request
) -> dict[str, Any]:
    user = await db.get(User, payload.user_id)
    if user is None:
        raise UnprocessableError("User not found")
    if (
        payload.telegram_user_id
        and payload.telegram_user_id not in request.app.state.settings.telegram_allowed_user_ids
    ):
        raise UnprocessableError("Add the Telegram ID to TELEGRAM_ALLOWED_USER_IDS first")
    if payload.telegram_user_id:
        existing = await db.scalar(
            select(User.id).where(
                User.telegram_user_id == payload.telegram_user_id, User.id != user.id
            )
        )
        if existing:
            raise UnprocessableError("Telegram account is already linked")
    user.telegram_user_id = payload.telegram_user_id
    await record_audit(
        db,
        action="telegram.user.linked",
        actor_user_id=principal.user.id,
        resource_type="user",
        resource_id=user.id,
    )
    await db.commit()
    return {"user_id": str(user.id), "telegram_user_id": user.telegram_user_id}


@router.post("/setup")
async def setup(
    payload: WebhookSetup, principal: AdminPrincipal, request: Request
) -> dict[str, Any]:
    secret = request.app.state.settings.telegram_webhook_secret
    parsed = urlsplit(payload.url)
    if not secret or len(secret.get_secret_value()) < 32:
        raise UnprocessableError("Configure TELEGRAM_WEBHOOK_SECRET with at least 32 characters")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise UnprocessableError("Webhook must be a public HTTPS URL")
    await client(request).call(
        "setWebhook",
        {
            "url": payload.url,
            "secret_token": secret.get_secret_value(),
            "allowed_updates": ["message", "callback_query"],
        },
    )
    return {"configured": True}


@router.post("/webhook")
async def webhook(
    payload: TelegramUpdatePayload, db: DbSession, request: Request
) -> dict[str, bool]:
    settings = request.app.state.settings
    secret = settings.telegram_webhook_secret
    presented = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secret or not hmac.compare_digest(secret.get_secret_value(), presented):
        raise PermissionDeniedError("Invalid Telegram webhook secret")
    callback = payload.callback_query
    message = (callback or {}).get("message") if callback else payload.message
    sender = (callback or message or {}).get("from", {}).get("id")
    chat = (message or {}).get("chat", {})
    if not isinstance(sender, int) or not isinstance(chat.get("id"), int):
        return {"ok": True}
    user = await telegram.authenticated_user(db, sender, chat["id"], chat.get("type", ""))
    inserted = await db.scalar(
        insert(TelegramUpdate)
        .values(
            update_id=payload.update_id,
            telegram_user_id=sender,
            chat_id=chat["id"],
            status="received",
            response={},
        )
        .on_conflict_do_nothing(index_elements=[TelegramUpdate.update_id])
        .returning(TelegramUpdate.id)
    )
    row = (
        await db.scalars(
            select(TelegramUpdate)
            .where(TelegramUpdate.update_id == payload.update_id)
            .with_for_update()
        )
    ).one()
    if row.status == "sent":
        return {"ok": True}
    if inserted:
        text = (message or {}).get("text", "/help")
        if callback:
            data = callback.get("data", "")
            action, _, identifier = data.partition(":")
            text = f"/{action} {identifier}" if action in {"approve", "reject"} else "/help"
        try:
            async with db.begin_nested():
                row.response = await telegram.command(db, user, str(text)[:8000])
        except (AppError, ValueError) as exc:
            row.response = {
                "text": exc.message if isinstance(exc, AppError) else "Invalid command identifier"
            }
        row.status = "processed"
        row.response = {**row.response, "text": redact(row.response["text"])}
        await db.commit()
    transport = client(request)
    await transport.send(chat["id"], row.response["text"], row.response.get("keyboard"))
    if callback and callback.get("id"):
        await transport.call("answerCallbackQuery", {"callback_query_id": callback["id"]})
    row.status = "sent"
    await db.commit()
    return {"ok": True}
