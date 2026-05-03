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

    async def set_my_commands(self, commands: list[dict[str, str]]) -> dict[str, Any]:
        """Push the slash-command menu shown by Telegram clients.

        `commands` is a list of `{"command": "<name>", "description": "<text>"}`.
        Telegram replaces the previous list; pushing an empty list clears
        it. Names must match `^[a-z0-9_]{1,32}$`; descriptions 1–256 chars.
        """
        return await self._post("/setMyCommands", {"commands": commands})

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        return await self._post("/answerCallbackQuery", payload)

    # ----- polling-mode endpoints (Phase 6a) -----
    #
    # These three only fire when TELEGRAM_MODE=polling. They cannot
    # coexist with webhook mode in the Telegram Bot API: getUpdates
    # returns 409 Conflict whenever a webhook URL is set, so the
    # daemon calls delete_webhook at startup. get_webhook_info is
    # used for diagnostics ("are we currently in webhook mode?")
    # without mutating state.

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout_s: int = 25,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Long-poll for new updates.

        Telegram holds the connection open for up to `timeout_s`
        seconds waiting for a new update; if none arrives it returns
        an empty list. `offset` is the smallest update_id the caller
        wants to receive — passing `last_seen_id + 1` acks every
        previous update so it's never delivered again.

        `allowed_updates` filters which update types the server sends.
        We only handle text messages right now, so default to that —
        keeps the polling loop from waking up on edited messages,
        callbacks, channel posts, etc. that the dispatcher would
        ignore anyway.
        """
        payload: dict[str, Any] = {"timeout": timeout_s}
        if offset is not None:
            payload["offset"] = offset
        payload["allowed_updates"] = allowed_updates if allowed_updates is not None else ["message"]
        # Important: the HTTP request must outlast the long-poll wait.
        # The client's default timeout is 10s but we're asking Telegram
        # to hold for `timeout_s`; bump per-call to leave headroom for
        # the response body to come back even right at the deadline.
        result = await self._post("/getUpdates", payload, request_timeout=timeout_s + 10)
        # Telegram envelope: {"ok": true, "result": [...]} — _post
        # already raised on non-2xx, so just unwrap.
        updates: list[dict[str, Any]] = result.get("result", [])
        return updates

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> dict[str, Any]:
        """Clear any configured webhook so getUpdates can be called.

        Telegram's getUpdates returns 409 when a webhook is active,
        with no way to "use both" — it's strictly one or the other.
        The daemon calls this at startup so transitioning from
        webhook → polling is automatic, not a manual two-step.

        `drop_pending_updates=True` discards anything Telegram queued
        while in webhook mode — useful if you want to skip a backlog
        on switchover (e.g. during dev mode flapping).
        """
        return await self._post("/deleteWebhook", {"drop_pending_updates": drop_pending_updates})

    async def get_webhook_info(self) -> dict[str, Any]:
        """Inspect the currently-configured webhook (if any).

        Returns the `result` object directly, not the envelope — keys
        of interest for diagnostics: `url` (empty string when no
        webhook is set), `pending_update_count`, `last_error_message`.
        """
        result = await self._post("/getWebhookInfo", {})
        return result.get("result", {})

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        request_timeout: float | None = None,
    ) -> dict[str, Any]:
        # Per-call timeout override lets get_updates use a long timeout
        # while leaving everything else on the constructor default.
        kwargs: dict[str, Any] = {"json": payload}
        if request_timeout is not None:
            kwargs["timeout"] = request_timeout
        resp = await self._client.post(path, **kwargs)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data
