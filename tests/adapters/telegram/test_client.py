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
