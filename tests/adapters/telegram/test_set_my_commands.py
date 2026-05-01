from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from bot_cmder.adapters.telegram.client import TelegramClient


@pytest.mark.asyncio
@respx.mock
async def test_set_my_commands_posts_full_list():
    route = respx.post("https://api.telegram.org/botfake/setMyCommands").mock(
        return_value=Response(200, json={"ok": True, "result": True})
    )

    async with TelegramClient(token="fake") as client:
        await client.set_my_commands(
            [
                {"command": "help", "description": "Show available commands"},
                {"command": "health", "description": "HTTP healthcheck"},
            ]
        )

    body = json.loads(route.calls.last.request.read().decode())
    assert body == {
        "commands": [
            {"command": "help", "description": "Show available commands"},
            {"command": "health", "description": "HTTP healthcheck"},
        ]
    }


@pytest.mark.asyncio
@respx.mock
async def test_set_my_commands_with_empty_list_clears_menu():
    route = respx.post("https://api.telegram.org/botfake/setMyCommands").mock(
        return_value=Response(200, json={"ok": True, "result": True})
    )
    async with TelegramClient(token="fake") as client:
        await client.set_my_commands([])
    body = json.loads(route.calls.last.request.read().decode())
    assert body == {"commands": []}
