from __future__ import annotations

from typing import Any

import httpx


class TelegramClient:
    """Minimal async client for the Telegram Bot API.

    Always POSTs JSON bodies — never builds URLs by string-formatting
    user content (which is the bug the legacy modules/hook/model.py
    code shipped with).
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=f"{base_url}/bot{token}",
            timeout=timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> TelegramClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        return await self._post("/sendMessage", payload)

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        return await self._post("/answerCallbackQuery", payload)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.post(path, json=payload)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data
