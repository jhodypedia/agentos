"""Phase 10: operational status and searchable logs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.db.enums import EventLevel, TaskStatus, UserRole
from app.db.models import Approval, AuditLog, Task, TaskEvent
from app.services.users import create_user


async def _login(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.integration
async def test_ops_status_reports_real_resources(app_client: AsyncClient, db_session) -> None:
    admin = await create_user(
        db_session,
        email="ops-admin@example.com",
        password="ops-admin-password-123456",
        role=UserRole.ADMIN,
    )
    await db_session.commit()
    headers = await _login(app_client, admin.email, "ops-admin-password-123456")

    response = await app_client.get("/api/v1/ops/status", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["environment"] == "testing"
    assert body["components"]["database"] == "ok"
    assert body["resources"]["workspace_quota_bytes"] > 0
    assert body["resources"]["database_tasks"] == 0
    assert body["sandbox"]["image"]
    assert body["retention"]["task_events_days"] >= 0


@pytest.mark.integration
async def test_ops_status_counts_pending_approvals(app_client: AsyncClient, db_session) -> None:
    admin = await create_user(
        db_session,
        email="ops-approvals@example.com",
        password="ops-approvals-password-1",
        role=UserRole.ADMIN,
    )
    task = Task(
        title="Needs approval",
        description="",
        objective="check approvals",
        status=TaskStatus.RUNNING,
        created_by=admin.id,
    )
    db_session.add(task)
    await db_session.flush()
    db_session.add(
        Approval(
            task_id=task.id,
            title="Run command",
            action_type="run_high_risk_tool",
            payload={"tool": "run_command"},
        )
    )
    await db_session.commit()

    headers = await _login(app_client, admin.email, "ops-approvals-password-1")
    body = (await app_client.get("/api/v1/ops/status", headers=headers)).json()
    assert body["resources"]["pending_approvals"] == 1
    assert body["resources"]["oldest_pending_approval_seconds"] is not None


@pytest.mark.integration
async def test_ops_logs_search_matches_task_events(app_client: AsyncClient, db_session) -> None:
    admin = await create_user(
        db_session,
        email="ops-logs@example.com",
        password="ops-logs-password-123456",
        role=UserRole.ADMIN,
    )
    task = Task(
        title="Log task",
        description="",
        objective="search logs",
        status=TaskStatus.RUNNING,
        created_by=admin.id,
    )
    db_session.add(task)
    await db_session.flush()
    db_session.add_all(
        [
            TaskEvent(
                task_id=task.id,
                event_type="tool.error",
                level=EventLevel.ERROR,
                message="docker build failed",
                payload={"stage": "build"},
            ),
            TaskEvent(
                task_id=task.id,
                event_type="task.progress",
                level=EventLevel.INFO,
                message="iteration started",
                payload={},
            ),
        ]
    )
    await db_session.commit()
    headers = await _login(app_client, admin.email, "ops-logs-password-123456")

    # `source=task` keeps the audit rows the login itself writes out of the result.
    hit = await app_client.get(
        "/api/v1/ops/logs", params={"q": "docker", "source": "task"}, headers=headers
    )
    assert hit.status_code == 200, hit.text
    body = hit.json()
    assert body["total"] == 1
    assert body["items"][0]["message"] == "docker build failed"
    assert body["items"][0]["payload"] == {"stage": "build"}

    errors_only = await app_client.get(
        "/api/v1/ops/logs", params={"level": "error", "source": "task"}, headers=headers
    )
    assert [item["event_type"] for item in errors_only.json()["items"]] == ["tool.error"]


@pytest.mark.integration
async def test_ops_logs_merges_audit_rows_newest_first(app_client: AsyncClient, db_session) -> None:
    admin = await create_user(
        db_session,
        email="ops-merge@example.com",
        password="ops-merge-password-1234",
        role=UserRole.ADMIN,
    )
    task = Task(
        title="Merge task",
        description="",
        objective="merge logs",
        status=TaskStatus.RUNNING,
        created_by=admin.id,
    )
    db_session.add(task)
    await db_session.flush()
    now = datetime.now(UTC)
    db_session.add(
        TaskEvent(
            task_id=task.id,
            event_type="task.started",
            level=EventLevel.INFO,
            message="started",
            payload={},
            created_at=now - timedelta(minutes=5),
        )
    )
    db_session.add(
        AuditLog(
            actor_user_id=admin.id,
            action="settings.update",
            resource_type="system_setting",
            resource_id="execution.workspace_quota_mb",
            success=True,
            meta={"key": "execution.workspace_quota_mb"},
            created_at=now,
        )
    )
    await db_session.commit()
    headers = await _login(app_client, admin.email, "ops-merge-password-1234")

    body = (await app_client.get("/api/v1/ops/logs", params={"limit": 3}, headers=headers)).json()
    # The login is itself audited, so the newest row is `auth.login`; what matters
    # here is that audit and task rows are merged and ordered by time.
    assert [item["source"] for item in body["items"]][:3] == ["audit", "audit", "task"]
    timestamps = [item["created_at"] for item in body["items"]]
    assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.integration
async def test_ops_logs_rejects_unknown_level(app_client: AsyncClient, db_session) -> None:
    admin = await create_user(
        db_session,
        email="ops-bad-level@example.com",
        password="ops-bad-level-password-1",
        role=UserRole.ADMIN,
    )
    await db_session.commit()
    headers = await _login(app_client, admin.email, "ops-bad-level-password-1")
    response = await app_client.get("/api/v1/ops/logs", params={"level": "nope"}, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unprocessable_entity"


@pytest.mark.integration
async def test_ops_logs_requires_authentication(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/ops/logs")
    assert response.status_code == 401


@pytest.mark.integration
async def test_ops_logs_respects_pagination(app_client: AsyncClient, db_session) -> None:
    admin = await create_user(
        db_session,
        email="ops-page@example.com",
        password="ops-page-password-12345",
        role=UserRole.ADMIN,
    )
    task = Task(
        title="Page task",
        description="",
        objective="page logs",
        status=TaskStatus.RUNNING,
        created_by=admin.id,
    )
    db_session.add(task)
    await db_session.flush()
    db_session.add_all(
        [
            TaskEvent(
                task_id=task.id,
                event_type="task.progress",
                level=EventLevel.INFO,
                message=f"step {index}",
                payload={},
            )
            for index in range(5)
        ]
    )
    await db_session.commit()
    headers = await _login(app_client, admin.email, "ops-page-password-12345")

    first = (
        await app_client.get(
            "/api/v1/ops/logs", params={"limit": 2, "source": "task"}, headers=headers
        )
    ).json()
    second = (
        await app_client.get(
            "/api/v1/ops/logs",
            params={"limit": 2, "offset": 2, "source": "task"},
            headers=headers,
        )
    ).json()
    assert first["total"] == 5
    assert len(first["items"]) == 2
    assert len(second["items"]) == 2
    first_ids = {item["id"] for item in first["items"]}
    second_ids = {item["id"] for item in second["items"]}
    assert first_ids & second_ids == set()


@pytest.mark.integration
async def test_ops_logs_level_filter_applies_to_both_sources(
    app_client: AsyncClient, db_session
) -> None:
    """An `error` filter must not return successful audit rows.

    Audit rows have no level column; the endpoint derives one from `success`
    (info / warning). Filtering only the task side made `/ops/logs?level=error`
    return every audit row, so the filter appeared to do nothing.
    """
    admin = await create_user(
        db_session,
        email="ops-level@example.com",
        password="ops-level-password-1234",
        role=UserRole.ADMIN,
    )
    task = Task(
        title="Level task",
        description="",
        objective="level filter",
        status=TaskStatus.RUNNING,
        created_by=admin.id,
    )
    db_session.add(task)
    await db_session.flush()
    db_session.add_all(
        [
            TaskEvent(
                task_id=task.id,
                event_type="task.failed",
                level=EventLevel.ERROR,
                message="boom",
                payload={},
            ),
            TaskEvent(
                task_id=task.id,
                event_type="task.started",
                level=EventLevel.INFO,
                message="started",
                payload={},
            ),
            AuditLog(
                actor_user_id=admin.id,
                action="task.executed",
                resource_type="task",
                resource_id=str(task.id),
                success=True,
                meta={},
            ),
            AuditLog(
                actor_user_id=admin.id,
                action="task.failed",
                resource_type="task",
                resource_id=str(task.id),
                success=False,
                meta={},
            ),
        ]
    )
    await db_session.commit()
    headers = await _login(app_client, admin.email, "ops-level-password-1234")

    errors = (
        await app_client.get(
            "/api/v1/ops/logs", params={"level": "error", "limit": 50}, headers=headers
        )
    ).json()
    assert errors["total"] == 1
    assert errors["items"][0]["message"] == "boom"

    warnings = (
        await app_client.get(
            "/api/v1/ops/logs", params={"level": "warning", "limit": 50}, headers=headers
        )
    ).json()
    messages = {item["message"] for item in warnings["items"]}
    assert "boom" in messages
    assert "task.failed on task " + str(task.id) in messages
    assert all(item["level"] in ("warning", "error", "critical") for item in warnings["items"])

    debug = (
        await app_client.get(
            "/api/v1/ops/logs", params={"level": "debug", "limit": 50}, headers=headers
        )
    ).json()
    assert debug["total"] > warnings["total"]


@pytest.mark.integration
async def test_ops_logs_filters_by_task_without_dropping_the_task_source(
    app_client: AsyncClient, db_session
) -> None:
    """Regression: `task_id` used to disable the task source instead of narrowing it.

    Every task-scoped query returned an empty page, so a Logs page filtered to one
    task showed nothing even when that task had plenty of events.
    """
    admin = await create_user(
        db_session,
        email="ops-taskfilter@example.com",
        password="ops-taskfilter-password-12",
        role=UserRole.OPERATOR,
    )
    wanted = Task(title="wanted", objective="find me", status=TaskStatus.COMPLETED)
    other = Task(title="other", objective="not me", status=TaskStatus.COMPLETED)
    db_session.add_all([wanted, other])
    await db_session.flush()
    db_session.add_all(
        [
            TaskEvent(task_id=wanted.id, event_type="task.created", message="wanted event"),
            TaskEvent(task_id=other.id, event_type="task.created", message="other event"),
        ]
    )
    await db_session.commit()
    headers = await _login(app_client, admin.email, "ops-taskfilter-password-12")

    response = await app_client.get(
        "/api/v1/ops/logs", params={"task_id": str(wanted.id)}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert [item["message"] for item in body["items"]] == ["wanted event"]
