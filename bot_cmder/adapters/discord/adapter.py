"""DiscordAdapter — turn Discord Interactions into IncomingMessages.

Slash command schema for Phase 4 is intentionally flat: every
top-level chat command (`/help`, `/service`, `/runbook`, ...) gets
ONE Discord application command with a single optional `args`
STRING option (or `code` for `/otp`). The adapter flattens that
back into the `/cmd <args...>` text shape the dispatcher already
knows how to handle, so behavior matches Telegram one-to-one.

Sub-commands like `/service restart api --host gce` work because
the user types `restart api --host gce` into the `args:` field;
the adapter rebuilds the text as `/service restart api --host gce`
and the existing Router rewrite in the dispatcher takes it from
there. (A richer registration that uses Discord native sub-commands
would mean re-pushing schema every time a yaml action is added —
not worth the latency for the MVP.)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from bot_cmder.adapters.base import PlatformAdapter
from bot_cmder.adapters.discord.client import DiscordClient
from bot_cmder.adapters.discord.schemas import Interaction, InteractionType
from bot_cmder.core.events import IncomingMessage, OutgoingResponse, Platform, PlatformUser

if TYPE_CHECKING:
    pass


class DiscordAdapter(PlatformAdapter):
    platform = Platform.DISCORD

    def __init__(self, client: DiscordClient) -> None:
        self._client = client

    @property
    def client(self) -> DiscordClient:
        return self._client

    def parse(self, raw: Any) -> IncomingMessage | None:
        interaction = raw if isinstance(raw, Interaction) else Interaction.model_validate(raw)
        if interaction.type != InteractionType.APPLICATION_COMMAND:
            # PING (type 1) is answered inline in the router. Other
            # types (autocomplete, modal submit, message component)
            # aren't part of Phase 4's surface.
            return None
        if interaction.data is None or not interaction.data.name:
            return None
        caller = interaction.caller()
        if caller is None:
            return None

        parts: list[str] = [f"/{interaction.data.name}"]
        for opt in interaction.data.options or []:
            if opt.value is None:
                continue
            parts.append(str(opt.value))
        text = " ".join(parts)

        return IncomingMessage(
            platform=Platform.DISCORD,
            user=PlatformUser(
                platform=Platform.DISCORD,
                raw_id=caller.id,
                handle=caller.username,
                display_name=caller.global_name or caller.username,
            ),
            chat_id=interaction.channel_id or interaction.guild_id or "dm",
            text=text,
            message_id=interaction.id,
            raw=interaction.model_dump(),
            received_at=datetime.now(timezone.utc),
        )

    async def send(self, msg: IncomingMessage, resp: OutgoingResponse) -> None:
        token = msg.raw.get("token") if isinstance(msg.raw, dict) else None
        if not token:
            # Without the per-interaction token we can't reach the
            # @original webhook URL; nothing to do.
            return
        await self._client.patch_original_response(token, resp.text)
