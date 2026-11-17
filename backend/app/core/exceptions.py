"""Application error hierarchy mapped to RFC 7807-style JSON responses."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for expected, user-facing application errors."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "An unexpected error occurred"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        super().__init__(self.message)


class BadRequestError(AppError):
    status_code = 400
    code = "bad_request"
    message = "The request is invalid"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"
    message = "Authentication is required"


class PermissionDeniedError(AppError):
    status_code = 403
    code = "permission_denied"
    message = "You do not have permission to perform this action"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    message = "The requested resource was not found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    message = "The request conflicts with the current state"


class UnprocessableError(AppError):
    status_code = 422
    code = "unprocessable_entity"
    message = "The request could not be processed"


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"
    message = "Too many requests"


class ExternalServiceError(AppError):
    status_code = 502
    code = "external_service_error"
    message = "An upstream service failed"


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"
    message = "The service is temporarily unavailable"


class TokenError(UnauthorizedError):
    code = "invalid_token"
    message = "The token is invalid or expired"
