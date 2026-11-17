"""OpenAI-compatible chat completions adapter.

Covers OpenAI itself, OpenRouter, LM Studio, Ollama, vLLM and any other server
exposing the ``/chat/completions`` contract.
"""

from __future__ import annotations

import json
from typing import Any

from app.providers.base import (
    FinishReason,
    LLMRequest,
    LLMResponse,
    ToolCall,
    ToolSpec,
    Usage,
)
from app.providers.base_provider import BaseProvider
from app.providers.errors import ProviderError
from app.providers.pricing import apply_cost

_FINISH_REASONS: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "content_filter",
}


class OpenAICompatibleProvider(BaseProvider):
    provider_type = "openai_compatible"
    default_base_url = "https://api.openai.com/v1"

    def __init__(self, *, api_key: str | None = None, **kwargs: Any) -> None:
        super().__init__(api_key=api_key, **kwargs)

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    # ---------------------------------------------------------------- payload
    def build_payload(self, request: LLMRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [message.to_openai() for message in request.messages],
            "temperature": request.temperature,
            "top_p": request.top_p,
            "max_tokens": request.max_output_tokens,
        }
        if request.tools:
            payload["tools"] = [tool.to_openai() for tool in request.tools]
            payload["tool_choice"] = "auto"
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if request.stop:
            payload["stop"] = request.stop
        payload.update(request.extra)
        return payload

    # -------------------------------------------------------------- responses
    @staticmethod
    def parse_response(body: dict[str, Any]) -> LLMResponse:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("provider response contained no choices")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderError("provider response contained a malformed choice")

        message = choice.get("message") or {}
        content = message.get("content") or ""
        if not isinstance(content, str):
            content = str(content)

        tool_calls: list[ToolCall] = []
        for index, raw_call in enumerate(message.get("tool_calls") or []):
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function") or {}
            tool_calls.append(
                ToolCall(
                    id=str(raw_call.get("id") or f"call_{index}"),
                    name=str(function.get("name") or ""),
                    arguments=_parse_arguments(function.get("arguments")),
                )
            )

        usage_raw = body.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
            total_tokens=int(usage_raw.get("total_tokens") or 0),
        )

        finish = str(choice.get("finish_reason") or "stop")
        if tool_calls and finish in ("stop", "function_call"):
            finish = "tool_calls"

        model = str(body.get("model") or "")
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=apply_cost(model, usage),
            finish_reason=_FINISH_REASONS.get(finish, "stop"),
            model=model,
            request_id=body.get("id"),
            raw=body,
        )

    # ---------------------------------------------------------------- contract
    async def complete(self, request: LLMRequest) -> LLMResponse:
        body = await self._post(
            "/chat/completions",
            self.build_payload(request),
            timeout_seconds=request.timeout_seconds,
        )
        return self.parse_response(body)

    async def list_models(self) -> list[str]:
        body = await self._get("/models", timeout_seconds=20.0)
        data = body.get("data")
        if not isinstance(data, list):
            return []
        models: list[str] = []
        for entry in data:
            if isinstance(entry, dict) and entry.get("id"):
                models.append(str(entry["id"]))
        return sorted(models)


class OpenAIProvider(OpenAICompatibleProvider):
    provider_type = "openai"

    def __init__(self, *, api_key: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("base_url", "https://api.openai.com/v1")
        super().__init__(api_key=api_key, **kwargs)


class OpenRouterProvider(OpenAICompatibleProvider):
    provider_type = "openrouter"

    def __init__(self, *, api_key: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("base_url", "https://openrouter.ai/api/v1")
        extra_headers = dict(kwargs.pop("extra_headers", {}) or {})
        extra_headers.setdefault("HTTP-Referer", "https://github.com/agentos")
        extra_headers.setdefault("X-Title", "AgentOS")
        super().__init__(api_key=api_key, extra_headers=extra_headers, **kwargs)


class LocalProvider(OpenAICompatibleProvider):
    """Self-hosted OpenAI-compatible server that needs no credentials."""

    provider_type = "local"
    requires_api_key = False

    def __init__(self, *, api_key: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("base_url", "http://127.0.0.1:11434/v1")
        super().__init__(api_key=api_key, **kwargs)


def _parse_arguments(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ProviderError("model returned tool arguments that are not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ProviderError("model returned non-object tool arguments")
        return parsed
    if raw is None:
        return {}
    raise ProviderError("model returned an unsupported tool argument payload")


def tool_specs(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [tool.to_openai() for tool in tools]
