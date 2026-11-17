"""Phase 10: secret scanning, key rotation and retention pruning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.security import decrypt_secret, encrypt_secret, rotate_secret
from app.db.models import ModelProvider
from app.services.maintenance import (
    prune_old_records,
    rotate_encryption_keys,
    scan_path,
    scan_text,
)


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------- scanner


def test_scan_finds_cloud_keys_and_reports_location() -> None:
    text = "line one\nexport AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
    findings = scan_text(text, path="deploy.sh")
    assert len(findings) == 1
    assert findings[0].rule == "aws_access_key"
    assert findings[0].line == 2
    assert findings[0].severity == "high"


def test_scan_never_returns_the_secret_itself() -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    findings = scan_text(f"OPENAI={secret}\n")
    assert findings
    assert secret not in findings[0].preview
    assert findings[0].preview.endswith("**")


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("token: ghp_abcdefghijklmnopqrstuvwxyz0123456789", "github_token"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private_key_block"),
        ("postgresql://user:hunter2@db.internal:5432/app", "database_url_with_password"),
        ("xoxb-1234567890-abcdefghijkl", "slack_token"),
        ("123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1", "telegram_bot_token"),
    ],
)
def test_scan_recognises_common_credential_shapes(text: str, rule: str) -> None:
    rules = {finding.rule for finding in scan_text(text)}
    assert rule in rules


def test_scan_is_quiet_on_ordinary_code() -> None:
    code = (
        "def handler(password: str) -> None:\n    token = os.environ['API_KEY']\n    return None\n"
    )
    assert scan_text(code) == []


def test_scan_path_skips_noise_directories_and_binaries(tmp_path: Path) -> None:
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "leak.js").write_text("AKIAIOSFODNN7EXAMPLE\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nAKIAIOSFODNN7EXAMPLE")
    (tmp_path / "app.py").write_text("print('hello')\n")
    (tmp_path / "deploy.sh").write_text("export K=AKIAIOSFODNN7EXAMPLE\n")

    findings = scan_path(tmp_path)
    assert [finding.path for finding in findings] == [str(tmp_path / "deploy.sh")]


def test_scan_path_on_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert scan_path(tmp_path / "nope") == []


def test_scan_reports_at_most_one_finding_per_line() -> None:
    findings = scan_text("AKIAIOSFODNN7EXAMPLE and password = 'aaaaaaaaaaaaaaaaaaaa'\n")
    assert len(findings) == 1


# ---------------------------------------------------------------- key rotation


def test_rotation_makes_values_readable_under_the_new_key() -> None:
    old = _settings()
    ciphertext = encrypt_secret("provider-secret", settings=old)

    new_key = "l4Z9qgzvK2Zm0dE1oQ2Y6vJk8yq1oG7G1n8l5b3XkQ0="
    rotated_settings = _settings(
        encryption_key=new_key, encryption_key_previous=old.encryption_key.get_secret_value()
    )
    assert decrypt_secret(ciphertext, settings=rotated_settings) == "provider-secret"

    rotated, changed = rotate_secret(ciphertext, settings=rotated_settings)
    assert changed is True
    assert decrypt_secret(rotated, settings=rotated_settings) == "provider-secret"

    only_new = _settings(encryption_key=new_key)
    assert decrypt_secret(rotated, settings=only_new) == "provider-secret"
    with pytest.raises(ValueError):
        decrypt_secret(ciphertext, settings=only_new)


def test_rotation_is_idempotent() -> None:
    settings = _settings()
    ciphertext = encrypt_secret("value", settings=settings)
    rotated, changed = rotate_secret(ciphertext, settings=settings)
    assert changed is False
    assert rotated == ciphertext


def test_previous_keys_must_be_valid_fernet_keys() -> None:
    with pytest.raises(ValueError):
        _settings(encryption_key_previous="not-a-key")


@pytest.mark.integration
async def test_rotate_cli_service_reencrypts_provider_rows(db_session) -> None:
    old = _settings()
    new_key = "l4Z9qgzvK2Zm0dE1oQ2Y6vJk8yq1oG7G1n8l5b3XkQ0="
    provider = ModelProvider(
        name="Rotation provider",
        slug="rotation-provider",
        provider_type="openai",
        api_key_encrypted=encrypt_secret("sk-live-value", settings=old),
        default_model="gpt-4o-mini",
    )
    db_session.add(provider)
    await db_session.commit()

    rotated_settings = _settings(
        encryption_key=new_key, encryption_key_previous=old.encryption_key.get_secret_value()
    )
    report = await rotate_encryption_keys(db_session, rotated_settings)
    await db_session.commit()
    await db_session.refresh(provider)

    assert report["rotated"] == 1
    assert report["failed"] == 0
    rotated_settings_only_new = _settings(encryption_key=new_key)
    assert (
        decrypt_secret(provider.api_key_encrypted, settings=rotated_settings_only_new)
        == "sk-live-value"
    )


@pytest.mark.integration
async def test_rotate_reports_unreadable_rows_without_touching_them(db_session) -> None:
    settings = _settings()
    provider = ModelProvider(
        name="Broken provider",
        slug="broken-provider",
        provider_type="openai",
        api_key_encrypted="not-a-fernet-token",
        default_model="gpt-4o-mini",
    )
    db_session.add(provider)
    await db_session.commit()

    report = await rotate_encryption_keys(db_session, settings)
    assert report["failed"] == 1
    await db_session.refresh(provider)
    assert provider.api_key_encrypted == "not-a-fernet-token"


# --------------------------------------------------------------------- pruning


@pytest.mark.integration
async def test_prune_deletes_rows_past_the_retention_window(db_session) -> None:
    from app.db.models import Task, TaskEvent

    settings = _settings()
    settings.retention_task_events_days = 30

    task = Task(title="Prune", description="", objective="prune")
    db_session.add(task)
    await db_session.flush()
    old = datetime.now(UTC) - timedelta(days=45)
    db_session.add_all(
        [
            TaskEvent(
                task_id=task.id,
                event_type="task.progress",
                level="info",
                message="old",
                payload={},
                created_at=old,
            ),
            TaskEvent(
                task_id=task.id,
                event_type="task.progress",
                level="info",
                message="new",
                payload={},
            ),
        ]
    )
    await db_session.commit()

    report = await prune_old_records(db_session, settings)
    await db_session.commit()
    assert report.task_events == 1

    remaining = (await db_session.execute(TaskEvent.__table__.select())).all()
    assert len(remaining) == 1


@pytest.mark.integration
async def test_prune_keeps_everything_when_a_window_is_zero(db_session) -> None:
    from app.db.models import Task, TaskEvent

    settings = _settings()
    settings.retention_task_events_days = 0

    task = Task(title="Keep", description="", objective="keep")
    db_session.add(task)
    await db_session.flush()
    db_session.add(
        TaskEvent(
            task_id=task.id,
            event_type="task.progress",
            level="info",
            message="ancient",
            payload={},
            created_at=datetime.now(UTC) - timedelta(days=4000),
        )
    )
    await db_session.commit()

    report = await prune_old_records(db_session, settings)
    assert report.task_events == 0
