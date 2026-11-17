"""Anthropic Messages API adapter."""

from __future__ import annotations

import json
from typing import Any

from app.providers.base import FinishReason, LLMRequest, LLMResponse, ToolCall, Usage
from app.providers.base_provider import BaseProvider
from app.providers.errors import ProviderError
from app.providers.pricing import apply_cost

_API_VERSION = "2023-06-01"

_STOP_REASONS: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


class AnthropicProvider(BaseProvider):
    provider_type = "anthropic"
    default_base_url = "https://api.anthropic.com/v1"

    def __init__(self, *, api_key: str | None = None, **kwargs: Any) -> None:
        super().__init__(api_key=api_key, **kwargs)

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["x-api-key"] = self._api_key or ""
        headers["anthropic-version"] = _API_VERSION
        return headers

    # ---------------------------------------------------------------- payload
    def build_payload(self, request: LLMRequest) -> dict[str, Any]:
        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []

        for message in request.messages:
            if message.role == "system":
                if message.content:
                    system_parts.append(message.content)
                continue
            if message.role == "tool":
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id or "",
                                "content": message.content,
                            }
                        ],
                    }
                )
                continue
            if message.role == "assistant" and message.tool_calls:
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for call in message.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.arguments,
                        }
                    )
                messages.append({"role": "assistant", "content": blocks})
                continue
            messages.append({"role": message.role, "content": message.content})

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.top_p != 1.0:
            payload["top_p"] = request.top_p
        if request.tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in request.tools
            ]
        if request.stop:
            payload["stop_sequences"] = request.stop
        payload.update(request.extra)
        return payload

    # ------------------------------------------------------------- responses
    @staticmethod
    def parse_response(body: dict[str, Any]) -> LLMResponse:
        blocks = body.get("content")
        if not isinstance(blocks, list):
            raise ProviderError("anthropic response contained no content blocks")

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(str(block.get("text") or ""))
            elif block_type == "tool_use":
                arguments = block.get("input")
                if isinstance(arguments, dict):
                    parsed: dict[str, Any] = arguments
                elif isinstance(arguments, str):
                    try:
                        candidate = json.loads(arguments)
                    except json.JSONDecodeError as exc:
                        raise ProviderError(
                            "anthropic returned tool input that is not valid JSON"
                        ) from exc
                    parsed = candidate if isinstance(candidate, dict) else {}
                else:
                    parsed = {}
                tool_calls.append(
                    ToolCall(
                        id=str(block.get("id") or ""),
                        name=str(block.get("name") or ""),
                        arguments=parsed,
                    )
                )

        usage_raw = body.get("usage") or {}
        prompt_tokens = int(usage_raw.get("input_tokens") or 0)
        completion_tokens = int(usage_raw.get("output_tokens") or 0)
        usage = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )

        model = str(body.get("model") or "")
        stop_reason = str(body.get("stop_reason") or "end_turn")
        finish = _STOP_REASONS.get(stop_reason, "stop")
        if tool_calls:
            finish = "tool_calls"

        return LLMResponse(
            content="".join(text_parts).strip(),
            tool_calls=tool_calls,
            usage=apply_cost(model, usage),
            finish_reason=finish,
            model=model,
            request_id=body.get("id"),
            raw=body,
        )

    # ---------------------------------------------------------------- contract
    async def complete(self, request: LLMRequest) -> LLMResponse:
        body = await self._post(
            "/messages",
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
