"""Application configuration loaded from environment variables and .env."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "testing", "production"]
LogFormat = Literal["console", "json"]


class Settings(BaseSettings):
    """Typed, validated application settings.

    Values are read from environment variables (case-insensitive) and an
    optional ``.env`` file. Secrets are wrapped in :class:`SecretStr` so they
    are never accidentally rendered in logs or API responses.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ app
    app_name: str = "AgentOS"
    app_version: str = "0.1.0"
    environment: Environment = "development"
    debug: bool = False
    api_prefix: str = "/api/v1"

    # ------------------------------------------------------------- security
    secret_key: SecretStr = Field(min_length=32)
    encryption_key: SecretStr
    # Retired Fernet keys, comma separated. Data encrypted with them is still
    # readable during a rotation window; `agentos rotate-encryption-key`
    # re-encrypts everything with the primary key and then this can be cleared.
    encryption_key_previous: str | None = None
    jwt_issuer: str = "agentos"
    jwt_audience: str = "agentos-api"
    access_token_ttl_minutes: int = Field(default=60, ge=5, le=60 * 24 * 7)
    refresh_token_ttl_days: int = Field(default=30, ge=1, le=365)

    # ------------------------------------------------------------- database
    database_url: str = "postgresql+asyncpg://agentos:agentos@localhost:5432/agentos"
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=100)
    database_echo: bool = False
    database_pool_recycle_seconds: int = Field(default=1800, ge=60)

    # ---------------------------------------------------------------- redis
    redis_url: str = "redis://localhost:6379/0"
    redis_socket_timeout_seconds: float = Field(default=5.0, gt=0)

    # ------------------------------------------------------------------ web
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )
    request_id_header: str = "X-Request-ID"

    # ---------------------------------------------------------------- media
    log_level: str = "INFO"
    log_format: LogFormat = "console"

    # ------------------------------------------------------ observability
    # Prometheus text exposition at /metrics. Off by default because the
    # endpoint is unauthenticated by design (a scraper cannot hold a JWT) and
    # reveals route names and error rates; turn it on when the port is only
    # reachable from the monitoring network.
    metrics_enabled: bool = True
    # Bearer token a scraper may present instead of relying on network
    # isolation. Empty means "no token required".
    metrics_token: SecretStr | None = None
    # Slow-request log threshold. Requests above it are logged at WARNING.
    slow_request_ms: int = Field(default=2_000, ge=100)

    # ---------------------------------------------------------- rate limits
    rate_limit_enabled: bool = True
    rate_limit_requests: int = Field(default=240, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    # Stricter bucket for authentication, where guessing is the threat.
    auth_rate_limit_requests: int = Field(default=10, ge=1)
    auth_rate_limit_window_seconds: int = Field(default=300, ge=1, le=3600)
    rate_limit_headers: bool = True
    # Honour X-Forwarded-For. Only enable behind a proxy that overwrites it;
    # otherwise a client can choose its own bucket.
    trust_forwarded_for: bool = False

    # --------------------------------------------------------- persistence
    # Days of task events / audit rows / model usage kept by the maintenance
    # job. 0 disables pruning for that table.
    retention_task_events_days: int = Field(default=30, ge=0)
    retention_audit_days: int = Field(default=180, ge=0)
    retention_model_usage_days: int = Field(default=365, ge=0)

    # -------------------------------------------------------------- sandbox
    workspace_root: Path = Path("/data/workspaces")
    sandbox_enabled: bool = True
    # Image used when commands run in Docker. It must contain the toolchain a
    # coding task needs; the default is a small image with git and a shell.
    sandbox_image: str = "agentos/sandbox:latest"
    security_sandbox_image: str = "agentos/research:latest"
    # With a sandbox requested but Docker unavailable, host execution is a
    # deliberate fallback rather than a silent one. Set to false in production
    # to refuse command execution outright instead.
    sandbox_allow_host_execution: bool = True
    # Hard ceiling for a single command, regardless of what a tool asks for.
    sandbox_max_timeout_seconds: int = Field(default=900, ge=1, le=7200)
    sandbox_memory_limit: str = "1g"
    sandbox_cpu_limit: float = Field(default=1.0, gt=0, le=64)
    sandbox_pids_limit: int = Field(default=256, ge=16, le=8192)
    # Disk quota enforced by the workspace before a write or a command runs.
    workspace_quota_mb: int = Field(default=5120, ge=1)
    coding_engine: str = "opencode"
    coding_max_turns: int = Field(default=30, ge=1, le=200)

    # --------------------------------------------------------------- skills
    skills_root: Path = Path("/data/skills")
    skill_max_file_bytes: int = Field(default=256_000, ge=1_024)
    skill_git_timeout_seconds: int = Field(default=120, ge=10, le=600)

    # --------------------------------------------------------------- memory
    memory_max_context_chars: int = Field(default=8_000, ge=500, le=64_000)
    memory_max_items: int = Field(default=40, ge=1, le=500)
    memory_retention_days: int = Field(default=180, ge=1)
    memory_half_life_days: float = Field(default=30.0, gt=0)
    # Embeddings are optional. Without a configured provider/model, memory
    # retrieval uses lexical scoring only and no vector is written.
    memory_embedding_provider: str | None = None
    memory_embedding_model: str | None = None
    memory_embedding_dimensions: int = Field(default=1536, ge=8, le=8192)
    memory_embedding_timeout_seconds: float = Field(default=20.0, gt=0)

    # --------------------------------------------------------------- bounty
    # make.fun is the only bounty platform with a documented agent interface.
    # The key is optional because every read route works without one; writes
    # (entering a competition) fail with a clear 401 until it is set.
    makefun_api_key: SecretStr | None = None
    makefun_base_url: str = "https://make.fun"
    makefun_timeout_seconds: float = Field(default=30.0, gt=0, le=180)
    makefun_sync_limit: int = Field(default=25, ge=1, le=100)

    # ------------------------------------------------------------- telegram
    telegram_bot_token: SecretStr | None = None
    telegram_allowed_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    telegram_webhook_secret: SecretStr | None = None
    telegram_notifications_enabled: bool = True
    # How often the bot process re-scans committed task events and pending
    # approvals for messages that have not been delivered yet.
    telegram_notify_interval_seconds: int = Field(default=30, ge=5, le=3600)

    # ---------------------------------------------------------------- worker
    # Seconds between polls when the queue is empty. Small enough that a new
    # task starts promptly, large enough not to hammer the database.
    worker_poll_interval_seconds: float = Field(default=2.0, gt=0, le=300)
    # A claimed task holds a lease for this long. The worker extends it while it
    # runs; if the process dies the lease lapses and another worker picks the
    # task up. Must be comfortably longer than one heartbeat interval.
    worker_lease_seconds: int = Field(default=300, ge=30, le=86_400)
    # Port the worker exposes /metrics on. It is a separate listener from the
    # API because the API's own /metrics describes the API process.
    worker_metrics_port: int = Field(default=9101, ge=1, le=65_535)

    # ------------------------------------------------------------ bootstrap
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: SecretStr | None = None

    @field_validator("cors_origins", "telegram_allowed_user_ids", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Allow comma-separated env values in addition to JSON arrays."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return json.loads(stripped)
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return value

    @field_validator(
        "telegram_bot_token",
        "telegram_webhook_secret",
        "makefun_api_key",
        "bootstrap_admin_password",
        "metrics_token",
        mode="before",
    )
    @classmethod
    def _blank_secret_is_none(cls, value: object) -> object:
        """Treat an empty env value as "not configured" rather than a secret."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("encryption_key")
    @classmethod
    def _validate_fernet_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        try:
            Fernet(raw.encode())
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "ENCRYPTION_KEY must be a valid Fernet key "
                '(python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())")'
            ) from exc
        return value

    @field_validator("encryption_key_previous")
    @classmethod
    def _validate_previous_keys(cls, value: str | None) -> str | None:
        if value is None:
            return None
        keys = [item.strip() for item in value.split(",") if item.strip()]
        if not keys:
            return None
        for key in keys:
            try:
                Fernet(key.encode())
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    "ENCRYPTION_KEY_PREVIOUS must be a comma-separated list of Fernet keys"
                ) from exc
        return ",".join(keys)

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}")
        return level

    @field_validator("api_prefix")
    @classmethod
    def _validate_prefix(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("API_PREFIX must start with '/'")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _production_hardening(self) -> Settings:
        if self.environment == "production":
            if self.debug:
                raise ValueError("DEBUG must be false in production")
            if any(origin == "*" for origin in self.cors_origins):
                raise ValueError("Wildcard CORS origins are not allowed in production")
            if not self.rate_limit_enabled:
                raise ValueError("RATE_LIMIT_ENABLED must not be false in production")
            if not self.sandbox_enabled and self.sandbox_allow_host_execution:
                raise ValueError(
                    "Production must either sandbox commands or refuse host execution: set "
                    "SANDBOX_ENABLED=true or SANDBOX_ALLOW_HOST_EXECUTION=false"
                )
        return self

    @property
    def previous_encryption_keys(self) -> tuple[str, ...]:
        raw = self.encryption_key_previous
        if not raw:
            return ()
        return tuple(item.strip() for item in raw.split(",") if item.strip())

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_testing(self) -> bool:
        return self.environment == "testing"

    @property
    def sync_database_url(self) -> str:
        """Return a psycopg-compatible URL (used only by tooling that needs it)."""
        return self.database_url.replace("+asyncpg", "")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
