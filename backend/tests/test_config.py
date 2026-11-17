from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _base(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "secret_key": "x" * 40,
        "encryption_key": "NCDnI89x2g-H2KQgWEKrJtxEEJXV_W-JCGDiaWCsee8=",
        "cors_origins": ["http://localhost:3000"],
    }
    values.update(overrides)
    return Settings(**values)


def test_cors_from_csv_string() -> None:
    settings = _base(cors_origins="http://a.test, http://b.test")
    assert settings.cors_origins == ["http://a.test", "http://b.test"]


def test_cors_from_json_string() -> None:
    settings = _base(cors_origins='["http://a.test"]')
    assert settings.cors_origins == ["http://a.test"]


def test_telegram_ids_from_csv_string() -> None:
    settings = _base(telegram_allowed_user_ids="1,2,3")
    assert settings.telegram_allowed_user_ids == [1, 2, 3]


def test_invalid_encryption_key_rejected() -> None:
    with pytest.raises(ValidationError):
        _base(encryption_key="not-a-fernet-key")


def test_short_secret_key_rejected() -> None:
    with pytest.raises(ValidationError):
        _base(secret_key="short")


def test_production_forbids_debug_and_wildcard_cors() -> None:
    with pytest.raises(ValidationError):
        _base(environment="production", debug=True)
    with pytest.raises(ValidationError):
        _base(environment="production", cors_origins=["*"])


def test_api_prefix_normalised() -> None:
    settings = _base(api_prefix="/api/v1/")
    assert settings.api_prefix == "/api/v1"


def test_sync_database_url() -> None:
    settings = _base(database_url="postgresql+asyncpg://u:p@h:5432/d")
    assert settings.sync_database_url == "postgresql://u:p@h:5432/d"
