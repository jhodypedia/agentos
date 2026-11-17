"""Minimal async Bot API transport; token-bearing URLs never enter application errors."""

from __future__ import annotations

from typing import Any

import httpx

from app.core.exceptions import ExternalServiceError


class TelegramClient:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._token = token
        self._client = client

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        if method not in {
            "sendMessage",
            "answerCallbackQuery",
            "setWebhook",
            "getWebhookInfo",
            "getMe",
        }:
            raise ValueError("Unsupported Telegram method")
        try:
            if self._client is not None:
                response = await self._client.post(
                    f"https://api.telegram.org/bot{self._token}/{method}", json=payload
                )
            else:
                async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                    response = await client.post(
                        f"https://api.telegram.org/bot{self._token}/{method}", json=payload
                    )
            body = response.json()
        except (httpx.HTTPError, ValueError):
            raise ExternalServiceError("Telegram transport failed") from None
        if (
            not 200 <= response.status_code < 300
            or not isinstance(body, dict)
            or not body.get("ok")
        ):
            raise ExternalServiceError(
                "Telegram refused the request",
                details={
                    "status_code": response.status_code,
                    "retry_after": body.get("parameters", {}).get("retry_after")
                    if isinstance(body, dict)
                    else None,
                },
            )
        return body.get("result")

    async def send(self, chat_id: int, text: str, keyboard: dict[str, Any] | None = None) -> None:
        from app.memory.sensitivity import redact

        clean = redact(text)
        # UTF-16 conservative bound; plain text avoids markup injection.
        for offset in range(0, len(clean), 1900):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": clean[offset : offset + 1900],
                "protect_content": True,
            }
            if keyboard and offset == 0:
                payload["reply_markup"] = keyboard
            await self.call("sendMessage", payload)
