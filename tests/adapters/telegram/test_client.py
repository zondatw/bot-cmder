from __future__ import annotations

import pytest
import respx
from httpx import Response

from bot_cmder.adapters.telegram.client import TelegramClient


@pytest.mark.asyncio
@respx.mock
async def test_send_message_uses_post_with_json_body():
    """Regression test for the legacy bug #1.

    The old bot_cmder/modules/hook/model.py built URLs by string-formatting
    user-provided text into the query string, which broke on `&`, spaces,
    and unicode. This test pins the contract: send_message MUST POST a
    JSON body, with the chat_id and text in the body, never in the URL.
    """
    route = respx.post("https://api.telegram.org/botfake/sendMessage").mock(
        return_value=Response(200, json={"ok": True, "result": {}})
    )

    async with TelegramClient(token="fake") as client:
        await client.send_message(
            chat_id=42,
            text="hello & friends 你好 = world",
            reply_to_message_id=7,
        )

    assert route.called
    call = route.calls.last
    assert call.request.method == "POST"
    # Critical: text must NOT be in the URL.
    assert "hello" not in str(call.request.url)
    assert "你好" not in str(call.request.url)
    body = call.request.read().decode()
    assert "hello & friends" in body
    assert "你好" in body
    assert '"chat_id": 42' in body or '"chat_id":42' in body
    assert '"reply_to_message_id": 7' in body or '"reply_to_message_id":7' in body


@pytest.mark.asyncio
@respx.mock
async def test_answer_callback_query_passes_id_and_text():
    """Regression test for legacy bug #2.

    answerCallbackQuery requires the callback_query.id (a short opaque
    string), NOT the user id. The legacy router accidentally passed
    callback_query.from_.id. This test pins the contract on the client
    side; Phase 5 (when buttons return) will add a router-level test.
    """
    route = respx.post("https://api.telegram.org/botfake/answerCallbackQuery").mock(
        return_value=Response(200, json={"ok": True, "result": True})
    )

    async with TelegramClient(token="fake") as client:
        await client.answer_callback_query(callback_query_id="abc-xyz", text="ok")

    body = route.calls.last.request.read().decode()
    assert '"callback_query_id": "abc-xyz"' in body or '"callback_query_id":"abc-xyz"' in body


# --- Phase 6a: polling-mode endpoints ---------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_get_updates_passes_offset_and_timeout_in_body():
    """Pins the long-poll contract: offset + timeout + allowed_updates
    all in the JSON body (not the URL — same lesson as send_message)."""
    route = respx.post("https://api.telegram.org/botfake/getUpdates").mock(
        return_value=Response(200, json={"ok": True, "result": []})
    )
    async with TelegramClient(token="fake") as client:
        await client.get_updates(offset=42, timeout_s=25)

    body = route.calls.last.request.read().decode()
    assert '"offset": 42' in body or '"offset":42' in body
    assert '"timeout": 25' in body or '"timeout":25' in body
    # Default filter narrows to "message" so we don't wake on
    # edited_message / channel_post / etc. that the dispatcher ignores.
    assert '"message"' in body


@pytest.mark.asyncio
@respx.mock
async def test_get_updates_unwraps_envelope_to_result_list():
    """Telegram returns {"ok": true, "result": [...]}; client returns the list."""
    fake_update = {"update_id": 100, "message": {"message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"}}}
    respx.post("https://api.telegram.org/botfake/getUpdates").mock(
        return_value=Response(200, json={"ok": True, "result": [fake_update]})
    )
    async with TelegramClient(token="fake") as client:
        updates = await client.get_updates()

    assert isinstance(updates, list)
    assert len(updates) == 1
    assert updates[0]["update_id"] == 100


@pytest.mark.asyncio
@respx.mock
async def test_get_updates_uses_long_request_timeout():
    """The HTTP request must outlast the long-poll wait — otherwise
    the client's default 10s timeout kills connections that Telegram
    is happily holding for 25s. Pin it: timeout passed to httpx is
    `timeout_s + 10` to leave headroom for the response body."""
    captured: dict = {}

    def _capture(request):
        # The httpx Request object exposes .extensions which carries
        # per-call overrides like timeout. Pull from there.
        captured["timeout"] = request.extensions.get("timeout")
        return Response(200, json={"ok": True, "result": []})

    respx.post("https://api.telegram.org/botfake/getUpdates").mock(side_effect=_capture)
    async with TelegramClient(token="fake") as client:
        await client.get_updates(timeout_s=25)

    # httpx wraps timeouts in a dict {connect, read, write, pool}.
    # We care about read (long-poll wait); should be 25 + 10 = 35.
    assert captured["timeout"]["read"] == 35


@pytest.mark.asyncio
@respx.mock
async def test_delete_webhook_hits_right_endpoint_with_drop_flag():
    route = respx.post("https://api.telegram.org/botfake/deleteWebhook").mock(
        return_value=Response(200, json={"ok": True, "result": True})
    )
    async with TelegramClient(token="fake") as client:
        await client.delete_webhook(drop_pending_updates=True)

    assert route.called
    body = route.calls.last.request.read().decode()
    assert '"drop_pending_updates": true' in body or '"drop_pending_updates":true' in body


@pytest.mark.asyncio
@respx.mock
async def test_get_webhook_info_returns_unwrapped_result():
    respx.post("https://api.telegram.org/botfake/getWebhookInfo").mock(
        return_value=Response(
            200,
            json={
                "ok": True,
                "result": {"url": "https://example.com/hook", "pending_update_count": 3},
            },
        )
    )
    async with TelegramClient(token="fake") as client:
        info = await client.get_webhook_info()

    assert info["url"] == "https://example.com/hook"
    assert info["pending_update_count"] == 3
