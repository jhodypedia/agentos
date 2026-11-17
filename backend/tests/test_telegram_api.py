from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.enums import UserRole
from app.db.models import Agent, Task
from app.db.session import get_session_factory
from app.services.users import create_user

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def configured(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[list[str]]:
    from app.telegram.client import TelegramClient

    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "t" * 40)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    get_settings.cache_clear()
    sent: list[str] = []

    async def send(self: TelegramClient, chat_id: int, text: str, keyboard: object = None) -> None:
        sent.append(text)

    monkeypatch.setattr(TelegramClient, "send", send)
    yield sent
    get_settings.cache_clear()


def update(identifier: int, text: str, sender: int = 123, chat_type: str = "private") -> dict:
    return {
        "update_id": identifier,
        "message": {
            "from": {"id": sender},
            "chat": {"id": sender, "type": chat_type},
            "text": text,
        },
    }


async def test_webhook_auth_and_unlinked_accounts(app_client: AsyncClient) -> None:
    route = "/api/v1/telegram/webhook"
    headers = {"X-Telegram-Bot-Api-Secret-Token": "t" * 40}
    assert (await app_client.post(route, json=update(1, "/start"))).status_code == 403
    assert (
        await app_client.post(route, headers=headers, json=update(1, "/start", 999))
    ).status_code == 403
    assert (
        await app_client.post(route, headers=headers, json=update(1, "/start"))
    ).status_code == 403


async def test_duplicate_update_does_not_create_two_tasks(
    app_client: AsyncClient, configured: list[str]
) -> None:
    async with get_session_factory()() as db:
        user = await create_user(
            db, email="telegram@example.com", password="Str0ng-Passw0rd!", role=UserRole.OPERATOR
        )
        user.telegram_user_id = 123
        db.add(Agent(slug="telegram-test", name="Test", system_prompt="Test"))
        await db.commit()
    payload = update(2, "/tasks create telegram-test Review local code")
    for _ in range(2):
        response = await app_client.post(
            "/api/v1/telegram/webhook",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "t" * 40},
        )
        assert response.status_code == 200, response.text
    async with get_session_factory()() as db:
        assert await db.scalar(select(func.count(Task.id))) == 1
    assert len(configured) == 1


async def test_viewer_cannot_create_tasks(app_client: AsyncClient, configured: list[str]) -> None:
    async with get_session_factory()() as db:
        user = await create_user(
            db, email="viewer-tg@example.com", password="Str0ng-Passw0rd!", role=UserRole.VIEWER
        )
        user.telegram_user_id = 123
        await db.commit()
    response = await app_client.post(
        "/api/v1/telegram/webhook",
        json=update(3, "/tasks create anything test"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "t" * 40},
    )
    assert response.status_code == 200
    assert "operator" in configured[0]


async def test_group_chat_is_rejected(app_client: AsyncClient) -> None:
    response = await app_client.post(
        "/api/v1/telegram/webhook",
        json=update(4, "/start", chat_type="group"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "t" * 40},
    )
    assert response.status_code == 403


async def test_callback_cannot_decide_another_users_assigned_approval(
    app_client: AsyncClient, configured: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.approvals import create_approval
    from app.telegram.client import TelegramClient

    async def call(*args: object, **kwargs: object) -> dict:
        return {}

    monkeypatch.setattr(TelegramClient, "call", call)
    async with get_session_factory()() as db:
        user = await create_user(
            db, email="callback@example.com", password="Str0ng-Passw0rd!", role=UserRole.OPERATOR
        )
        user.telegram_user_id = 123
        other = await create_user(
            db, email="other@example.com", password="Str0ng-Passw0rd!", role=UserRole.OPERATOR
        )
        approval = await create_approval(db, title="Restricted", assigned_to_user_id=other.id)
        await db.commit()
        identifier = str(approval.id)
    response = await app_client.post(
        "/api/v1/telegram/webhook",
        json={
            "update_id": 5,
            "callback_query": {
                "id": "query-1",
                "from": {"id": 123},
                "data": f"approve:{identifier}",
                "message": {"chat": {"id": 123, "type": "private"}},
            },
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "t" * 40},
    )
    assert response.status_code == 200, response.text
    assert "another user" in configured[0]
