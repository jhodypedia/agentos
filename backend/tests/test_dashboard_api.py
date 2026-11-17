from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.db.enums import ApprovalStatus, TaskStatus, UserRole
from app.db.models import Approval, ModelUsage, Task
from app.db.session import get_session_factory
from app.services.audit import record_audit
from app.services.users import create_user

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def configured(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv("REDIS_URL", get_settings().redis_url)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def headers(client: AsyncClient, email: str, role: UserRole) -> dict[str, str]:
    async with get_session_factory()() as db:
        await create_user(db, email=email, password="Str0ng-Passw0rd!", role=role)
        await db.commit()
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "Str0ng-Passw0rd!"}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_empty_install_reports_zeroes_not_fabrications(app_client: AsyncClient) -> None:
    viewer = await headers(app_client, "dash-viewer@example.com", UserRole.VIEWER)
    response = await app_client.get("/api/v1/dashboard/summary", headers=viewer)
    assert response.status_code == 200
    body = response.json()
    assert body["agents_total"] == 0
    assert body["tasks_total"] == 0
    assert body["approvals_pending"] == 0
    assert body["total_cost_usd"] == "0"
    assert [item for item in body["activity"] if item["action"] != "auth.login"] == []
    assert body["health"]["database"] == "ok"


async def test_summary_and_usage_reflect_persisted_rows(app_client: AsyncClient) -> None:
    operator = await headers(app_client, "dash-op@example.com", UserRole.OPERATOR)
    async with get_session_factory()() as db:
        user_id = (
            await create_user(
                db,
                email="dash-data@example.com",
                password="Str0ng-Passw0rd!",
                role=UserRole.OPERATOR,
            )
        ).id
        db.add(Task(title="One", objective="Do", status=TaskStatus.RUNNING, created_by=user_id))
        db.add(Task(title="Two", objective="Do", status=TaskStatus.COMPLETED, created_by=user_id))
        db.add(Approval(title="Approve", status=ApprovalStatus.PENDING))
        db.add(ModelUsage(model_name="m", total_tokens=120, cost_usd=Decimal("0.5")))
        await record_audit(
            db,
            action="task.created",
            resource_type="task",
            resource_id=uuid4(),
            actor_user_id=user_id,
        )
        await db.commit()

    summary = await app_client.get("/api/v1/dashboard/summary", headers=operator)
    body = summary.json()
    assert body["tasks_by_status"]["running"] == 1
    assert body["tasks_total"] == 2
    assert body["approvals_pending"] == 1
    assert body["total_tokens"] == 120
    assert body["total_cost_usd"] == "0.500000"
    assert next(item["action"] for item in body["activity"]) == "task.created"

    # `days=1` means today. Regression: `since` used to be the current instant,
    # so a row written earlier today fell outside the window and the series came
    # back empty for the most commonly requested range.
    usage = await app_client.get("/api/v1/dashboard/usage?days=1", headers=operator)
    assert usage.status_code == 200
    series = usage.json()
    assert series["total_requests"] == 1, series
    assert series["total_tokens"] == 120
    assert len(series["points"]) == 1


async def test_viewer_cannot_read_event_stream_without_token(app_client: AsyncClient) -> None:
    viewer = await headers(app_client, "dash-v@example.com", UserRole.VIEWER)
    response = await app_client.get("/api/v1/dashboard/events", headers=viewer)
    assert response.status_code == 403
    assert (await app_client.get("/api/v1/dashboard/activity")).status_code == 401


async def test_event_stream_requires_operator_token(app_client: AsyncClient) -> None:
    viewer = await headers(app_client, "dash-stream@example.com", UserRole.VIEWER)
    token = viewer["Authorization"].split(" ", 1)[1]
    response = await app_client.get(f"/api/v1/dashboard/stream?token={token}")
    assert response.status_code == 403

    operator = await headers(app_client, "dash-stream-op@example.com", UserRole.OPERATOR)
    operator_token = operator["Authorization"].split(" ", 1)[1]
    assert (await app_client.get("/api/v1/dashboard/stream?token=not-a-token")).status_code == 401
    assert operator_token
