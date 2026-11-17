from __future__ import annotations

import json

import httpx
import pytest

from app.core.exceptions import ExternalServiceError
from app.telegram.client import TelegramClient


async def test_transport_redacts_credentials_and_splits_messages() -> None:
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        await TelegramClient("test-token", transport).send(
            123, "api_key=secret-value " + "x" * 4000
        )
    assert len(sent) == 3
    assert "secret-value" not in json.dumps(sent)
    assert all(len(message["text"]) <= 1900 for message in sent)


async def test_transport_errors_do_not_leak_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("token-in-url", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        with pytest.raises(ExternalServiceError) as error:
            await TelegramClient("private-token", transport).send(123, "test")
    assert "private-token" not in str(error.value)


async def test_transport_does_not_claim_success_on_api_error() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": False}))
    ) as transport:
        with pytest.raises(ExternalServiceError):
            await TelegramClient("test", transport).send(123, "test")
