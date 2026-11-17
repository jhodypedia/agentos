"""OpenAI Responses API adapter.

Some OpenAI-compatible gateways expose only ``/responses`` rather than the
legacy ``/chat/completions`` contract, and may additionally require a client
``originator`` header. This adapter speaks that shape: system messages become
``instructions``, the conversation becomes a list of input items, tool
definitions are flat, and tool results are ``function_call_output`` items.
"""

from __future__ import annotations

import json
from typing import Any

from app.providers.base import (
    ChatMessage,
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

_INCOMPLETE_REASONS: dict[str, FinishReason] = {
    "max_output_tokens": "length",
    "content_filter": "content_filter",
}


class CodexResponsesProvider(BaseProvider):
    """Adapter for gateways that only serve the Responses API.

    ``originator`` is configurable through the provider's ``extra_headers``;
    it defaults to ``codex_cli_rs`` because gateways commonly fingerprint the
    calling client before routing a request.
    """

    provider_type = "codex_responses"
    default_base_url = "https://api.openai.com/v1"

    def __init__(self, *, api_key: str | None = None, **kwargs: Any) -> None:
        extra_headers = dict(kwargs.pop("extra_headers", {}) or {})
        extra_headers.setdefault("originator", "codex_cli_rs")
        extra_headers.setdefault("User-Agent", "codex_cli_rs/0.48.0")
        super().__init__(api_key=api_key, extra_headers=extra_headers, **kwargs)

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        headers.setdefault("OpenAI-Beta", "responses=experimental")
        return headers

    # ---------------------------------------------------------------- payload
    @staticmethod
    def _tool_to_item(tool: ToolSpec) -> dict[str, Any]:
        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }

    @staticmethod
    def _message_item(message: ChatMessage) -> dict[str, Any] | None:
        """Convert one turn into Responses input items (a tool turn can yield several)."""
        if message.role == "tool":
            return {
                "type": "function_call_output",
                "call_id": message.tool_call_id or "",
                "output": message.content or "",
            }
        if message.role == "assistant":
            if message.tool_calls:
                # Tool calls are separate items; the assistant's own text, if any,
                # is dropped because the runtime only keeps tool calls on those
                # turns.
                return None
            return {
                "role": "assistant",
                "content": [{"type": "output_text", "text": message.content}],
            }
        return {
            "role": "user",
            "content": [{"type": "input_text", "text": message.content}],
        }

    def build_payload(self, request: LLMRequest) -> dict[str, Any]:
        instructions: list[str] = []
        items: list[dict[str, Any]] = []

        for message in request.messages:
            if message.role == "system":
                if message.content:
                    instructions.append(message.content)
                continue
            if message.role == "assistant" and message.tool_calls:
                items.extend(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    }
                    for call in message.tool_calls
                )
                continue
            item = self._message_item(message)
            if item is not None:
                items.append(item)

        payload: dict[str, Any] = {
            "model": request.model,
            "input": items,
            "temperature": request.temperature,
            "top_p": request.top_p,
            "max_output_tokens": request.max_output_tokens,
        }
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        if request.tools:
            payload["tools"] = [self._tool_to_item(tool) for tool in request.tools]
            payload["tool_choice"] = "auto"
        if request.json_mode:
            payload["text"] = {"format": {"type": "json_object"}}
        payload.update(request.extra)
        return payload

    # -------------------------------------------------------------- responses
    @staticmethod
    def parse_response(body: dict[str, Any]) -> LLMResponse:
        if body.get("error"):
            error = body["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ProviderError(str(message or "provider returned an error"))

        output = body.get("output")
        if not isinstance(output, list):
            raise ProviderError("provider response contained no output")

        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") in (
                        "output_text",
                        "text",
                    ):
                        text = part.get("text")
                        if isinstance(text, str):
                            content_parts.append(text)
            elif item_type == "function_call":
                tool_calls.append(
                    ToolCall(
                        id=str(item.get("call_id") or item.get("id") or ""),
                        name=str(item.get("name") or ""),
                        arguments=_parse_arguments(item.get("arguments")),
                    )
                )

        usage_raw = body.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("input_tokens") or 0),
            completion_tokens=int(usage_raw.get("output_tokens") or 0),
            total_tokens=int(usage_raw.get("total_tokens") or 0),
        )

        finish: FinishReason = "tool_calls" if tool_calls else "stop"
        status = body.get("status")
        if status == "incomplete":
            details = body.get("incomplete_details") or {}
            reason = details.get("reason") if isinstance(details, dict) else None
            finish = _INCOMPLETE_REASONS.get(str(reason), "length")
        elif status == "failed":
            error = body.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else None
            raise ProviderError(str(message or "provider reported a failed response"))

        model = str(body.get("model") or "")
        return LLMResponse(
            content="".join(content_parts),
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
            "/responses",
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
            raise ProviderError("provider returned malformed tool arguments") from exc
        if isinstance(parsed, dict):
            return parsed
        raise ProviderError("provider returned non-object tool arguments")
    if raw is None:
        return {}
    raise ProviderError("provider returned malformed tool arguments")
