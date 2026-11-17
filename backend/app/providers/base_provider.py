"""Abstract provider contract and shared HTTP behaviour."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

import httpx
import structlog

from app.providers.base import LLMRequest, LLMResponse, ProviderHealth
from app.providers.errors import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)

logger = structlog.get_logger("agentos.providers")

_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class BaseProvider(ABC):
    """A model provider adapter.

    Subclasses translate :class:`LLMRequest` into their wire format and back.
    Retries, error mapping and timeouts live here so every adapter behaves the
    same way.
    """

    provider_type: str = "unknown"
    default_base_url: str = ""
    requires_api_key: bool = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        if self.requires_api_key and not api_key:
            raise ProviderConfigError(f"{self.provider_type} requires an API key")
        self._api_key = api_key
        self._base_url = (base_url or self.default_base_url).rstrip("/")
        if not self._base_url:
            raise ProviderConfigError(f"{self.provider_type} requires a base URL")
        self._timeout = timeout_seconds
        self._max_retries = max(0, max_retries)
        self._extra_headers = extra_headers or {}
        self._client = client
        self._owns_client = client is None

    # ---------------------------------------------------------------- helpers
    @property
    def base_url(self) -> str:
        return self._base_url

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(self._extra_headers)
        return headers

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> BaseProvider:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # -------------------------------------------------------------- transport
    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        client = self._client_or_create()
        timeout = timeout_seconds or self._timeout
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await client.post(
                    url,
                    json=payload,
                    headers=self._headers(),
                    timeout=timeout,
                )
            except httpx.TimeoutException as exc:
                last_error = ProviderTimeoutError(
                    f"{self.provider_type} timed out after {timeout:.0f}s"
                )
                logger.warning("provider_timeout", provider=self.provider_type, attempt=attempt)
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise ProviderTimeoutError from exc
            except httpx.HTTPError as exc:
                last_error = ProviderError(f"{self.provider_type} transport error: {exc}")
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise ProviderError from exc

            if response.status_code < 400:
                return self._parse_json(response)

            detail = _error_detail(response)
            if response.status_code in (401, 403):
                raise ProviderAuthError(detail)
            if response.status_code == 429:
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_after(response, attempt))
                    continue
                raise ProviderRateLimitError(detail)
            if response.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                await asyncio.sleep(self._backoff(attempt))
                continue
            raise ProviderError(f"{detail} (HTTP {response.status_code})")

        raise ProviderError(str(last_error) if last_error else "provider request failed")

    async def _get(self, path: str, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        client = self._client_or_create()
        try:
            response = await client.get(
                f"{self._base_url}{path}",
                headers=self._headers(),
                timeout=timeout_seconds or self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"{self.provider_type} timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.provider_type} transport error: {exc}") from exc
        if response.status_code in (401, 403):
            raise ProviderAuthError(_error_detail(response))
        if response.status_code >= 400:
            raise ProviderError(f"{_error_detail(response)} (HTTP {response.status_code})")
        return self._parse_json(response)

    @staticmethod
    def _parse_json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("provider returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise ProviderError("provider returned an unexpected payload")
        return body

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2.0**attempt * 0.5, 8.0)

    @staticmethod
    def _retry_after(response: httpx.Response, attempt: int) -> float:
        raw = response.headers.get("retry-after")
        if raw:
            try:
                return min(float(raw), 30.0)
            except ValueError:
                pass
        return min(2.0**attempt, 10.0)

    # ---------------------------------------------------------------- contract
    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Run a single completion and return a normalised response."""

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Return the models the provider advertises."""

    async def health(self) -> ProviderHealth:
        """Cheap credential/connectivity check."""
        started = asyncio.get_running_loop().time()
        try:
            models = await self.list_models()
        except ProviderAuthError as exc:
            return ProviderHealth(ok=False, detail=str(exc))
        except ProviderError as exc:
            return ProviderHealth(ok=False, detail=str(exc))
        latency = int((asyncio.get_running_loop().time() - started) * 1000)
        return ProviderHealth(ok=True, detail="ok", models=models, latency_ms=latency)


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:400] or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code")
            if message:
                return str(message)[:400]
        if isinstance(error, str):
            return error[:400]
        message = body.get("message")
        if isinstance(message, str):
            return message[:400]
    return f"HTTP {response.status_code}"
