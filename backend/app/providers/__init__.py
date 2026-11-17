"""Model provider adapters.

Adapters translate a single :class:`~app.providers.base.LLMRequest` into a
vendor-specific call and normalise the response, so the runtime never depends
on a specific vendor SDK.
"""

from app.providers.base import (
    ChatMessage,
    FinishReason,
    LLMRequest,
    LLMResponse,
    MessageRole,
    ProviderHealth,
    ToolCall,
    ToolSpec,
    Usage,
)
from app.providers.base_provider import BaseProvider
from app.providers.errors import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    UnsupportedCapabilityError,
)
from app.providers.registry import (
    build_provider,
    provider_from_record,
    register,
    registered_types,
)

__all__ = [
    "BaseProvider",
    "ChatMessage",
    "FinishReason",
    "LLMRequest",
    "LLMResponse",
    "MessageRole",
    "ProviderAuthError",
    "ProviderConfigError",
    "ProviderError",
    "ProviderHealth",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "ToolCall",
    "ToolSpec",
    "UnsupportedCapabilityError",
    "Usage",
    "build_provider",
    "provider_from_record",
    "register",
    "registered_types",
]
