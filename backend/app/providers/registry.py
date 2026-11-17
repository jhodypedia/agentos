"""Provider registry: construct adapters from database records.

Nothing in the runtime hardcodes a vendor. ``build_provider`` maps a
``ModelProvider`` row (type + base URL + decrypted key) to an adapter, and
``register`` lets tests and plugins add new adapter classes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from app.core.security import decrypt_secret
from app.db.enums import ProviderType
from app.providers.anthropic import AnthropicProvider
from app.providers.base_provider import BaseProvider
from app.providers.codex_responses import CodexResponsesProvider
from app.providers.errors import ProviderConfigError
from app.providers.google import GoogleProvider
from app.providers.openai_compatible import (
    LocalProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    OpenRouterProvider,
)

if TYPE_CHECKING:
    from app.db.models import ModelProvider

ProviderFactory = Callable[..., BaseProvider]

_REGISTRY: dict[str, ProviderFactory] = {}


def register(provider_type: str, factory: ProviderFactory) -> None:
    """Register (or replace) an adapter factory for a provider type."""
    _REGISTRY[provider_type] = factory


def registered_types() -> list[str]:
    return sorted(_REGISTRY)


def build_provider(
    provider_type: ProviderType | str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 120.0,
    max_retries: int = 2,
    extra_headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> BaseProvider:
    """Instantiate an adapter for ``provider_type``."""
    key = str(getattr(provider_type, "value", provider_type))
    factory = _REGISTRY.get(key)
    if factory is None:
        raise ProviderConfigError(f"Unsupported provider type '{key}'")
    return factory(
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        extra_headers=extra_headers,
        **kwargs,
    )


def provider_from_record(
    record: ModelProvider,
    *,
    timeout_seconds: float | None = None,
    max_retries: int = 2,
) -> BaseProvider:
    """Build an adapter for a stored provider, decrypting its API key.

    The plaintext key exists only in memory for the lifetime of the adapter.
    """
    if not record.enabled:
        raise ProviderConfigError(f"Provider '{record.slug}' is disabled")

    api_key: str | None = None
    if record.api_key_encrypted:
        try:
            api_key = decrypt_secret(record.api_key_encrypted)
        except ValueError as exc:
            raise ProviderConfigError(
                f"Provider '{record.slug}' has an unreadable API key; re-enter it"
            ) from exc

    config = record.config or {}
    timeout = timeout_seconds or float(config.get("timeout_seconds", 120))
    headers = config.get("extra_headers")
    if headers is not None and not isinstance(headers, dict):
        headers = None

    return build_provider(
        record.provider_type,
        api_key=api_key,
        base_url=record.base_url,
        timeout_seconds=timeout,
        max_retries=max_retries,
        extra_headers={str(k): str(v) for k, v in (headers or {}).items()},
    )


register(ProviderType.OPENAI.value, OpenAIProvider)
register(ProviderType.ANTHROPIC.value, AnthropicProvider)
register(ProviderType.GOOGLE.value, GoogleProvider)
register(ProviderType.OPENROUTER.value, OpenRouterProvider)
register(ProviderType.LOCAL.value, LocalProvider)
register(ProviderType.OPENAI_COMPATIBLE.value, OpenAICompatibleProvider)
register(ProviderType.CODEX_RESPONSES.value, CodexResponsesProvider)
