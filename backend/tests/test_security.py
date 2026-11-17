from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from app.core.config import get_settings
from app.core.exceptions import TokenError
from app.core.security import (
    constant_time_equals,
    create_access_token,
    decode_access_token,
    decrypt_secret,
    encrypt_secret,
    generate_api_key,
    hash_api_key,
    hash_password,
    validate_password_strength,
    verify_password,
)


def test_password_hash_roundtrip() -> None:
    digest = hash_password("correct horse battery staple")
    assert digest != "correct horse battery staple"
    assert verify_password("correct horse battery staple", digest)
    assert not verify_password("wrong password", digest)


def test_password_policy() -> None:
    assert validate_password_strength("Str0ng-Passw0rd!") == "Str0ng-Passw0rd!"
    for weak in ["short", "alllowercaseonly", "1234567890123456"]:
        with pytest.raises(ValueError):
            validate_password_strength(weak)


def test_jwt_roundtrip_and_claims() -> None:
    user_id = uuid4()
    token, claims = create_access_token(user_id, "owner", scopes=["*"])
    decoded = decode_access_token(token)
    assert decoded.subject == user_id
    assert decoded.role == "owner"
    assert decoded.scopes == ("*",)
    assert decoded.jwt_id == claims.jwt_id
    assert decoded.expires_at > datetime.now(UTC)


def test_expired_token_rejected() -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "role": "viewer",
            "type": "access",
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": int((now - timedelta(hours=2)).timestamp()),
            "exp": int((now - timedelta(hours=1)).timestamp()),
        },
        settings.secret_key.get_secret_value(),
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        decode_access_token(token)


def test_tampered_token_rejected() -> None:
    token, _ = create_access_token(uuid4(), "viewer")
    with pytest.raises(TokenError):
        decode_access_token(token + "x")


def test_wrong_token_type_rejected() -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "role": "viewer",
            "type": "refresh",
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        },
        settings.secret_key.get_secret_value(),
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        decode_access_token(token)


def test_secret_encryption_roundtrip() -> None:
    ciphertext = encrypt_secret("sk-live-super-secret")
    assert ciphertext != "sk-live-super-secret"
    assert decrypt_secret(ciphertext) == "sk-live-super-secret"
    with pytest.raises(ValueError):
        decrypt_secret("not-a-token")


def test_api_key_generation_and_hashing() -> None:
    key = generate_api_key()
    assert key.plaintext.startswith("aos_")
    assert key.prefix == key.plaintext[:12]
    assert len(key.key_hash) == 64
    assert constant_time_equals(key.key_hash, hash_api_key(key.plaintext))
    assert not constant_time_equals(key.key_hash, hash_api_key("aos_other"))
