"""Password hashing, JWT issuing/verification, secret encryption and API keys."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import Settings, get_settings
from app.core.exceptions import TokenError

_API_KEY_PREFIX = "aos_"
_PASSWORD_HASHER = PasswordHasher()


# --------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    """Hash a plaintext password with Argon2id."""
    if not password:
        raise ValueError("password must not be empty")
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against an Argon2 hash."""
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _PASSWORD_HASHER.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def validate_password_strength(password: str) -> str:
    """Enforce the platform password policy and return the password."""
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters long")
    if len(password) > 256:
        raise ValueError("Password must be at most 256 characters long")
    categories = 0
    categories += bool(re.search(r"[a-z]", password))
    categories += bool(re.search(r"[A-Z]", password))
    categories += bool(re.search(r"\d", password))
    categories += bool(re.search(r"[^A-Za-z0-9]", password))
    if categories < 3:
        raise ValueError(
            "Password must contain at least three of: lowercase, uppercase, digit, symbol"
        )
    return password


# -------------------------------------------------------------------------- JWT
@dataclass(frozen=True, slots=True)
class TokenClaims:
    subject: UUID
    role: str
    scopes: tuple[str, ...]
    token_type: str
    issued_at: datetime
    expires_at: datetime
    jwt_id: str


def create_access_token(
    subject: UUID,
    role: str,
    *,
    scopes: list[str] | None = None,
    settings: Settings | None = None,
) -> tuple[str, TokenClaims]:
    """Create a signed HS256 access token and return it with its claims."""
    cfg = settings or get_settings()
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=cfg.access_token_ttl_minutes)
    jwt_id = secrets.token_hex(16)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "role": role,
        "scopes": scopes or [],
        "type": "access",
        "iss": cfg.jwt_issuer,
        "aud": cfg.jwt_audience,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": jwt_id,
    }
    token = jwt.encode(
        payload,
        cfg.secret_key.get_secret_value(),
        algorithm="HS256",
    )
    claims = TokenClaims(
        subject=subject,
        role=role,
        scopes=tuple(scopes or []),
        token_type="access",
        issued_at=now,
        expires_at=expires_at,
        jwt_id=jwt_id,
    )
    return token, claims


def decode_access_token(token: str, *, settings: Settings | None = None) -> TokenClaims:
    """Verify and decode an access token, raising :class:`TokenError` on failure."""
    cfg = settings or get_settings()
    try:
        payload = jwt.decode(
            token,
            cfg.secret_key.get_secret_value(),
            algorithms=["HS256"],
            audience=cfg.jwt_audience,
            issuer=cfg.jwt_issuer,
            options={"require": ["sub", "exp", "iat", "iss", "aud", "type"]},
        )
    except jwt.InvalidTokenError as exc:
        raise TokenError() from exc

    if payload.get("type") != "access":
        raise TokenError("Only access tokens are accepted here")
    try:
        subject = UUID(str(payload["sub"]))
        issued_at = datetime.fromtimestamp(int(payload["iat"]), tz=UTC)
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
    except (KeyError, ValueError) as exc:
        raise TokenError() from exc

    scopes = payload.get("scopes") or []
    if not isinstance(scopes, list):
        scopes = []
    return TokenClaims(
        subject=subject,
        role=str(payload.get("role", "viewer")),
        scopes=tuple(str(scope) for scope in scopes),
        token_type="access",
        issued_at=issued_at,
        expires_at=expires_at,
        jwt_id=str(payload.get("jti", "")),
    )


# ------------------------------------------------------------------- encryption
def _multi_fernet(settings: Settings | None = None) -> MultiFernet:
    """Primary key first, then any retired keys still allowed to decrypt.

    ``MultiFernet.encrypt`` always uses the first key, so new writes land on the
    current key while :func:`decrypt_secret` can still read values written
    before a rotation.
    """
    cfg = settings or get_settings()
    keys = [cfg.encryption_key.get_secret_value(), *cfg.previous_encryption_keys]
    return MultiFernet([Fernet(key.encode()) for key in keys])


def encrypt_secret(plaintext: str, *, settings: Settings | None = None) -> str:
    """Encrypt a secret (e.g. a provider API key) for storage at rest."""
    return _multi_fernet(settings).encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str, *, settings: Settings | None = None) -> str:
    """Decrypt a value produced by :func:`encrypt_secret`."""
    try:
        return _multi_fernet(settings).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Unable to decrypt secret: invalid key or payload") from exc


def rotate_secret(ciphertext: str, *, settings: Settings | None = None) -> tuple[str, bool]:
    """Re-encrypt a value under the primary key.

    Returns the new ciphertext and whether the value actually needed rotating.
    Fernet encryption is randomised, so comparing ciphertexts would report a
    change every time; instead the primary key alone is asked to decrypt, and a
    value it can already read is returned untouched.
    """
    cfg = settings or get_settings()
    primary = Fernet(cfg.encryption_key.get_secret_value().encode())
    try:
        primary.decrypt(ciphertext.encode())
    except InvalidToken:
        plaintext = _multi_fernet(cfg).decrypt(ciphertext.encode())
        return primary.encrypt(plaintext).decode(), True
    return ciphertext, False


# --------------------------------------------------------------------- API keys
@dataclass(frozen=True, slots=True)
class GeneratedApiKey:
    plaintext: str
    prefix: str
    key_hash: str


def generate_api_key() -> GeneratedApiKey:
    """Generate an API key. Only the hash and prefix are persisted."""
    raw = secrets.token_urlsafe(32)
    plaintext = f"{_API_KEY_PREFIX}{raw}"
    return GeneratedApiKey(
        plaintext=plaintext,
        prefix=plaintext[:12],
        key_hash=hash_api_key(plaintext),
    )


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)
