"""Minimal async Discord REST client.

Two-purpose:

  1. Reply to a deferred interaction by PATCHing the @original
     message via the interaction's webhook URL. These endpoints are
     keyed by interaction_token and don't take the bot's Bearer auth
     — they're effectively per-interaction one-shot URLs.
  2. Register the application's slash command schema (PUT
     /applications/{id}/commands or .../guilds/{guild_id}/commands).
     These endpoints DO need the bot token in `Authorization: Bot`.

Discord caps message content at 2000 chars; longer replies are
truncated with a `[...truncated]` marker so the operator knows.
"""

from __future__ import annotations

from typing import Any

import httpx

_DISCORD_MAX_MESSAGE_CHARS = 2000


class DiscordClient:
    BASE_URL = "https://discord.com/api/v10"

    def __init__(
        self,
        *,
        bot_token: str,
        application_id: str,
        base_url: str = BASE_URL,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._application_id = application_id
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> DiscordClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def patch_original_response(
        self,
        interaction_token: str,
        content: str,
    ) -> dict[str, Any]:
        """Replace a deferred interaction's @original message body.

        No bot-token auth: the interaction_token itself authorizes
        the call. Discord's content limit is 2000 chars; we truncate
        beyond that with a marker so reply contents that come back
        from a chatty SSH command don't silently disappear.
        """
        url = f"/webhooks/{self._application_id}/{interaction_token}/messages/@original"
        truncated = _truncate(content)
        resp = await self._client.patch(url, json={"content": truncated})
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def create_message(self, channel_id: str, content: str) -> dict[str, Any]:
        """POST a chat message to a channel.

        Used by Phase 6c Gateway-mode replies — interactions reply via
        the per-invocation webhook URL (no auth needed), but Gateway-
        originated messages don't have one, so we go through the
        regular bot-authed REST endpoint. Same 2000-char truncation
        the @original PATCH path uses.
        """
        url = f"/channels/{channel_id}/messages"
        truncated = _truncate(content)
        resp = await self._client.post(
            url,
            json={"content": truncated},
            headers={"Authorization": f"Bot {self._bot_token}"},
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def overwrite_global_commands(self, commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """PUT-replace the application's global slash command set."""
        return await self._put_commands(f"/applications/{self._application_id}/commands", commands)

    async def overwrite_guild_commands(
        self,
        guild_id: str,
        commands: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """PUT-replace a guild's slash command set. Updates propagate instantly."""
        return await self._put_commands(
            f"/applications/{self._application_id}/guilds/{guild_id}/commands",
            commands,
        )

    async def _put_commands(self, url: str, commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
        resp = await self._client.put(
            url,
            json=commands,
            headers={"Authorization": f"Bot {self._bot_token}"},
        )
        resp.raise_for_status()
        data: list[dict[str, Any]] = resp.json()
        return data


def _truncate(content: str) -> str:
    if not content:
        return "(no output)"
    if len(content) <= _DISCORD_MAX_MESSAGE_CHARS:
        return content
    marker = "\n[...truncated]"
    return content[: _DISCORD_MAX_MESSAGE_CHARS - len(marker)] + marker
