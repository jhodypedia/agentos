"""Tests for provider adapters, payload translation and pricing."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.providers.anthropic import AnthropicProvider
from app.providers.base import ChatMessage, LLMRequest, ToolCall, ToolSpec
from app.providers.codex_responses import CodexResponsesProvider
from app.providers.errors import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from app.providers.google import GoogleProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.pricing import calculate_cost, lookup_price
from app.providers.registry import build_provider, registered_types


def _mock_transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_registry_has_all_provider_types() -> None:
    assert set(registered_types()) == {
        "openai",
        "anthropic",
        "google",
        "openrouter",
        "local",
        "openai_compatible",
        "codex_responses",
    }


def test_unknown_provider_type_is_rejected() -> None:
    with pytest.raises(ProviderConfigError):
        build_provider("does-not-exist", api_key="x")


def test_missing_api_key_is_rejected() -> None:
    with pytest.raises(ProviderConfigError):
        build_provider("openai", api_key=None)


def test_local_provider_needs_no_key() -> None:
    provider = build_provider("local", base_url="http://127.0.0.1:9999/v1")
    assert provider.provider_type == "local"


def test_pricing_prefix_matching() -> None:
    price = lookup_price("gpt-4o-mini-2024-07-18")
    assert price is not None
    assert price.input_per_mtok == Decimal("0.15")
    assert lookup_price("unknown-model") is None


def test_cost_calculation() -> None:
    cost = calculate_cost("gpt-4o-mini", 1_000_000, 1_000_000)
    assert cost == Decimal("0.750000")
    assert calculate_cost("mystery-model", 1000, 1000) == Decimal("0")


async def test_openai_payload_and_parsing() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "message": {
                            "content": "hello",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "echo",
                                        "arguments": '{"text": "hi"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    async with _mock_transport(handler) as client:
        provider = OpenAICompatibleProvider(
            api_key="sk-test", base_url="https://api.example.com/v1", client=client
        )
        request = LLMRequest(
            model="gpt-4o-mini",
            messages=[ChatMessage(role="user", content="hi")],
            tools=[ToolSpec(name="echo", description="echo", parameters={"type": "object"})],
        )
        response = await provider.complete(request)

    assert captured["url"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer sk-test"
    assert captured["body"]["tools"][0]["function"]["name"] == "echo"
    assert response.content == "hello"
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0].name == "echo"
    assert response.tool_calls[0].arguments == {"text": "hi"}
    assert response.usage.total_tokens == 15
    assert response.usage.cost_usd > 0


async def test_openai_auth_error_is_mapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    async with _mock_transport(handler) as client:
        provider = OpenAICompatibleProvider(api_key="sk-x", base_url="https://x/v1", client=client)
        with pytest.raises(ProviderAuthError, match="bad key"):
            await provider.complete(LLMRequest(model="m", messages=[]))


async def test_rate_limit_after_retries() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "slow down"})

    async with _mock_transport(handler) as client:
        provider = OpenAICompatibleProvider(
            api_key="sk-x", base_url="https://x/v1", client=client, max_retries=1
        )
        with pytest.raises(ProviderRateLimitError):
            await provider.complete(LLMRequest(model="m", messages=[]))
    assert calls["n"] == 2


async def test_timeout_is_mapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    async with _mock_transport(handler) as client:
        provider = OpenAICompatibleProvider(
            api_key="sk-x", base_url="https://x/v1", client=client, max_retries=0
        )
        with pytest.raises(ProviderTimeoutError):
            await provider.complete(LLMRequest(model="m", messages=[]))


def test_anthropic_payload_translation() -> None:
    provider = AnthropicProvider(api_key="k")
    request = LLMRequest(
        model="claude-sonnet-4",
        messages=[
            ChatMessage(role="system", content="be terse"),
            ChatMessage(role="user", content="hi"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="t1", name="echo", arguments={"text": "a"})],
            ),
            ChatMessage(role="tool", content="ok", tool_call_id="t1", name="echo"),
        ],
        tools=[ToolSpec(name="echo", description="d", parameters={"type": "object"})],
    )
    payload = provider.build_payload(request)
    assert payload["system"] == "be terse"
    assert payload["messages"][0] == {"role": "user", "content": "hi"}
    assert payload["messages"][1]["content"][0]["type"] == "tool_use"
    assert payload["messages"][2]["content"][0]["type"] == "tool_result"
    assert payload["tools"][0]["input_schema"] == {"type": "object"}


def test_anthropic_response_parsing() -> None:
    body = {
        "id": "msg_1",
        "model": "claude-sonnet-4-20250514",
        "content": [
            {"type": "text", "text": "thinking..."},
            {"type": "tool_use", "id": "t1", "name": "echo", "input": {"text": "x"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
    response = AnthropicProvider.parse_response(body)
    assert response.content == "thinking..."
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0].arguments == {"text": "x"}
    assert response.usage.total_tokens == 120


def test_google_payload_and_parsing() -> None:
    provider = GoogleProvider(api_key="k")
    request = LLMRequest(
        model="gemini-2.5-flash",
        messages=[
            ChatMessage(role="system", content="sys"),
            ChatMessage(role="user", content="hello"),
        ],
        max_output_tokens=100,
    )
    payload = provider.build_payload(request)
    assert payload["systemInstruction"]["parts"][0]["text"] == "sys"
    assert payload["contents"][0]["role"] == "user"
    assert payload["generationConfig"]["maxOutputTokens"] == 100

    body = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "hi"},
                        {"functionCall": {"name": "echo", "args": {"text": "z"}}},
                    ]
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 7,
            "candidatesTokenCount": 3,
            "totalTokenCount": 10,
        },
        "modelVersion": "gemini-2.5-flash",
    }
    response = GoogleProvider.parse_response(body, "gemini-2.5-flash")
    assert response.content == "hi"
    assert response.tool_calls[0].arguments == {"text": "z"}
    assert response.usage.total_tokens == 10
    assert response.finish_reason == "tool_calls"


async def test_list_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "b"}, {"id": "a"}]})

    async with _mock_transport(handler) as client:
        provider = OpenAICompatibleProvider(api_key="k", base_url="https://x/v1", client=client)
        assert await provider.list_models() == ["a", "b"]


async def test_health_reports_auth_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "forbidden"}})

    async with _mock_transport(handler) as client:
        provider = OpenAICompatibleProvider(api_key="k", base_url="https://x/v1", client=client)
        health = await provider.health()
    assert health.ok is False
    assert "forbidden" in health.detail


def _codex_provider(handler) -> CodexResponsesProvider:
    return CodexResponsesProvider(
        api_key="sk-test",
        base_url="https://gateway.test/v1",
        client=_mock_transport(handler),
    )


def _responses_body(output: list[dict], *, status: str = "completed", **extra) -> dict:
    return {
        "id": "resp_1",
        "object": "response",
        "model": "gpt-6.1-sol",
        "status": status,
        "output": output,
        "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        **extra,
    }


@pytest.mark.asyncio
async def test_codex_responses_translates_history_and_system_prompt() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        captured["_path"] = request.url.path
        captured["_originator"] = request.headers.get("originator")
        return httpx.Response(200, json=_responses_body([]))

    provider = _codex_provider(handler)
    request = LLMRequest(
        model="gpt-6.1-sol",
        messages=[
            ChatMessage(role="system", content="You are terse."),
            ChatMessage(role="system", content="Stay factual."),
            ChatMessage(role="user", content="Plan it"),
            ChatMessage(
                role="assistant",
                tool_calls=[ToolCall(id="call_a", name="echo", arguments={"text": "hi"})],
            ),
            ChatMessage(role="tool", content="hi", tool_call_id="call_a"),
        ],
        tools=[ToolSpec(name="echo", description="Echo text", parameters={"type": "object"})],
        max_output_tokens=256,
        temperature=0.1,
    )
    await provider.complete(request)

    assert captured["_path"] == "/v1/responses"
    assert captured["_originator"] == "codex_cli_rs"
    assert captured["instructions"] == "You are terse.\n\nStay factual."
    assert captured["model"] == "gpt-6.1-sol"
    assert captured["max_output_tokens"] == 256
    assert captured["tools"] == [
        {
            "type": "function",
            "name": "echo",
            "description": "Echo text",
            "parameters": {"type": "object"},
        }
    ]
    items = captured["input"]
    assert items[0] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "Plan it"}],
    }
    assert items[1] == {
        "type": "function_call",
        "call_id": "call_a",
        "name": "echo",
        "arguments": '{"text": "hi"}',
    }
    assert items[2] == {
        "type": "function_call_output",
        "call_id": "call_a",
        "output": "hi",
    }


@pytest.mark.asyncio
async def test_codex_responses_parses_text_and_tool_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_responses_body(
                [
                    {"type": "reasoning", "summary": []},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "thinking..."}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_9",
                        "name": "echo",
                        "arguments": '{"text":"yo"}',
                    },
                ]
            ),
        )

    provider = _codex_provider(handler)
    response = await provider.complete(
        LLMRequest(model="gpt-6.1-sol", messages=[ChatMessage(role="user", content="hi")])
    )

    assert response.content == "thinking..."
    assert response.finish_reason == "tool_calls"
    assert [(c.id, c.name, c.arguments) for c in response.tool_calls] == [
        ("call_9", "echo", {"text": "yo"})
    ]
    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 7
    assert response.request_id == "resp_1"


@pytest.mark.asyncio
async def test_codex_responses_incomplete_length_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_responses_body(
                [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "partial"}],
                    }
                ],
                status="incomplete",
                incomplete_details={"reason": "max_output_tokens"},
            ),
        )

    provider = _codex_provider(handler)
    response = await provider.complete(
        LLMRequest(model="m", messages=[ChatMessage(role="user", content="hi")])
    )
    assert response.finish_reason == "length"


@pytest.mark.asyncio
async def test_codex_responses_surfaces_gateway_error() -> None:
    from app.providers.errors import ProviderError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "failed",
                "error": {"code": "client_not_allowed", "message": "not allowed"},
                "output": [],
            },
        )

    provider = _codex_provider(handler)
    with pytest.raises(ProviderError, match="not allowed"):
        await provider.complete(
            LLMRequest(model="m", messages=[ChatMessage(role="user", content="hi")])
        )


@pytest.mark.asyncio
async def test_codex_responses_malformed_tool_arguments_raise() -> None:
    from app.providers.errors import ProviderError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_responses_body(
                [{"type": "function_call", "call_id": "c", "name": "echo", "arguments": "{bad"}]
            ),
        )

    provider = _codex_provider(handler)
    with pytest.raises(ProviderError):
        await provider.complete(
            LLMRequest(model="m", messages=[ChatMessage(role="user", content="hi")])
        )


@pytest.mark.asyncio
async def test_codex_responses_lists_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"id": "b-model"}, {"id": "a-model"}], "object": "list"}
        )

    provider = _codex_provider(handler)
    assert await provider.list_models() == ["a-model", "b-model"]
