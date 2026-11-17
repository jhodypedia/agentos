"""Google Gemini (generateContent) adapter."""

from __future__ import annotations

from typing import Any

from app.providers.base import FinishReason, LLMRequest, LLMResponse, ToolCall, Usage
from app.providers.base_provider import BaseProvider
from app.providers.errors import ProviderError
from app.providers.pricing import apply_cost

_FINISH_REASONS: dict[str, FinishReason] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "OTHER": "error",
    "MALFORMED_FUNCTION_CALL": "error",
}


class GoogleProvider(BaseProvider):
    provider_type = "google"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        if self._api_key:
            headers["x-goog-api-key"] = self._api_key
        return headers

    # ---------------------------------------------------------------- payload
    def build_payload(self, request: LLMRequest) -> dict[str, Any]:
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []

        for message in request.messages:
            if message.role == "system":
                if message.content:
                    system_parts.append(message.content)
                continue
            if message.role == "tool":
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": message.name or "tool",
                                    "response": {"result": message.content},
                                }
                            }
                        ],
                    }
                )
                continue
            role = "model" if message.role == "assistant" else "user"
            parts: list[dict[str, Any]] = []
            if message.content:
                parts.append({"text": message.content})
            for call in message.tool_calls:
                parts.append({"functionCall": {"name": call.name, "args": call.arguments}})
            contents.append({"role": role, "parts": parts or [{"text": ""}]})

        generation_config: dict[str, Any] = {
            "temperature": request.temperature,
            "topP": request.top_p,
            "maxOutputTokens": request.max_output_tokens,
        }
        if request.json_mode:
            generation_config["responseMimeType"] = "application/json"
        if request.stop:
            generation_config["stopSequences"] = request.stop

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        if request.tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters,
                        }
                        for tool in request.tools
                    ]
                }
            ]
        payload.update(request.extra)
        return payload

    # ------------------------------------------------------------- responses
    @staticmethod
    def parse_response(body: dict[str, Any], requested_model: str = "") -> LLMResponse:
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ProviderError("gemini response contained no candidates")
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise ProviderError("gemini response contained a malformed candidate")

        content = candidate.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else None
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for index, part in enumerate(parts or []):
            if not isinstance(part, dict):
                continue
            if "text" in part:
                text_parts.append(str(part["text"]))
            call = part.get("functionCall")
            if isinstance(call, dict):
                args = call.get("args")
                tool_calls.append(
                    ToolCall(
                        id=f"call_{index}",
                        name=str(call.get("name") or ""),
                        arguments=args if isinstance(args, dict) else {},
                    )
                )

        usage_raw = body.get("usageMetadata") or {}
        prompt_tokens = int(usage_raw.get("promptTokenCount") or 0)
        completion_tokens = int(usage_raw.get("candidatesTokenCount") or 0)
        total_tokens = int(usage_raw.get("totalTokenCount") or 0) or (
            prompt_tokens + completion_tokens
        )
        usage = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

        raw_finish = str(candidate.get("finishReason") or "STOP")
        finish = _FINISH_REASONS.get(raw_finish, "stop")
        if tool_calls:
            finish = "tool_calls"

        model = str(body.get("modelVersion") or requested_model)
        return LLMResponse(
            content="".join(text_parts).strip(),
            tool_calls=tool_calls,
            usage=apply_cost(model, usage),
            finish_reason=finish,
            model=model,
            request_id=body.get("responseId"),
            raw=body,
        )

    # ---------------------------------------------------------------- contract
    async def complete(self, request: LLMRequest) -> LLMResponse:
        body = await self._post(
            f"/models/{request.model}:generateContent",
            self.build_payload(request),
            timeout_seconds=request.timeout_seconds,
        )
        return self.parse_response(body, request.model)

    async def list_models(self) -> list[str]:
        body = await self._get("/models", timeout_seconds=20.0)
        data = body.get("models")
        if not isinstance(data, list):
            return []
        models: list[str] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if name.startswith("models/"):
                name = name[len("models/") :]
            if name:
                models.append(name)
        return sorted(models)
