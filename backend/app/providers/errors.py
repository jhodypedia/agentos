"""Provider errors mapped onto the platform's HTTP error hierarchy."""

from __future__ import annotations

from app.core.exceptions import AppError, ExternalServiceError, UnprocessableError


class ProviderError(ExternalServiceError):
    code = "provider_error"
    message = "The model provider returned an error"


class ProviderAuthError(ProviderError):
    code = "provider_auth_error"
    message = "The model provider rejected the credentials"


class ProviderRateLimitError(ProviderError):
    code = "provider_rate_limited"
    message = "The model provider rate limit was hit"


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"
    message = "The model provider did not respond in time"


class ProviderConfigError(UnprocessableError):
    code = "provider_misconfigured"
    message = "The model provider is not configured correctly"


class UnsupportedCapabilityError(AppError):
    status_code = 400
    code = "unsupported_capability"
    message = "The provider does not support this capability"
