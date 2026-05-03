from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from bot_cmder.adapters.discord.client import DiscordClient


@pytest.mark.asyncio
@respx.mock
async def test_patch_original_response_uses_webhook_url_no_auth():
    """Discord's @original PATCH is the per-interaction webhook URL,
    keyed by interaction_token — no `Authorization: Bot` header. Pin
    it: an extra Bot header would still work but makes the auth model
    confusing for future readers."""
    route = respx.patch("https://discord.com/api/v10/webhooks/123/tok/messages/@original").mock(
        return_value=Response(200, json={"id": "msg1"})
    )

    async with DiscordClient(bot_token="bot-token", application_id="123") as client:
        await client.patch_original_response("tok", "hello world")

    request = route.calls.last.request
    assert request.method == "PATCH"
    body = json.loads(request.read())
    assert body == {"content": "hello world"}
    # No Authorization header should accompany the per-interaction URL.
    assert "authorization" not in {k.lower() for k in request.headers}


@pytest.mark.asyncio
@respx.mock
async def test_patch_original_response_truncates_at_2000_chars():
    """Discord caps message content at 2000 chars; longer replies must
    be truncated client-side with a marker, not silently dropped or
    sent (Discord would 400 on the over-length payload)."""
    route = respx.patch("https://discord.com/api/v10/webhooks/123/tok/messages/@original").mock(
        return_value=Response(200, json={"id": "msg1"})
    )

    async with DiscordClient(bot_token="bot-token", application_id="123") as client:
        await client.patch_original_response("tok", "x" * 5000)

    body = json.loads(route.calls.last.request.read())
    assert len(body["content"]) <= 2000
    assert "[...truncated]" in body["content"]


@pytest.mark.asyncio
@respx.mock
async def test_overwrite_global_commands_sends_bot_token():
    """The command-registration endpoints DO require the bot token —
    pin it so a future refactor that accidentally drops the header
    doesn't silently 401 every time we re-deploy."""
    route = respx.put("https://discord.com/api/v10/applications/123/commands").mock(
        return_value=Response(200, json=[{"id": "c1", "name": "help"}])
    )

    async with DiscordClient(bot_token="bot-token", application_id="123") as client:
        result = await client.overwrite_global_commands([{"name": "help", "description": "h", "type": 1}])

    request = route.calls.last.request
    assert request.method == "PUT"
    assert request.headers.get("authorization") == "Bot bot-token"
    body = json.loads(request.read())
    assert body == [{"name": "help", "description": "h", "type": 1}]
    assert result == [{"id": "c1", "name": "help"}]


@pytest.mark.asyncio
@respx.mock
async def test_overwrite_guild_commands_uses_guild_url():
    route = respx.put("https://discord.com/api/v10/applications/123/guilds/g1/commands").mock(
        return_value=Response(200, json=[])
    )

    async with DiscordClient(bot_token="bot-token", application_id="123") as client:
        await client.overwrite_guild_commands("g1", [])
    assert route.called
