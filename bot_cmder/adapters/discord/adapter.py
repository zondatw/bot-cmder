"""DiscordAdapter — Discord Interactions OR Gateway events → IncomingMessages.

Two ingestion paths feed the same dispatcher:

  - **HTTP Interactions** (Phase 4, default): slash commands arrive
    as the `Interaction` envelope. Schema is intentionally flat —
    one Discord application command per top-level chat command, with
    a single optional `args` STRING option. We rebuild that into the
    `/cmd <args...>` text shape Telegram produces, so behavior matches
    one-to-one.

  - **Gateway** (Phase 6c, `DISCORD_MODE=gateway`): chat messages
    arrive as `MESSAGE_CREATE` dispatch events over WSS. Discord does
    NOT deliver slash command interactions through the Gateway
    (platform limitation), so the UX shifts to "@bot cmd args" in
    guild channels OR plain `cmd args` in DMs. The adapter strips
    the `<@bot_id>` mention prefix and normalizes the result into the
    same `/cmd args...` shape the dispatcher expects.

Replies:
  - Interactions path → PATCH @original webhook URL (no bot-token auth)
  - Gateway path → POST /channels/{id}/messages (bot-token auth)

The two paths share `parse()` via type sniffing on the raw payload —
Interaction objects always have a `type` int field; MessageCreatePayload
always has `content` + `author`. Easier than splitting into two
adapters and saves the router/daemon from caring which path the
adapter chose.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from bot_cmder.adapters.base import PlatformAdapter
from bot_cmder.adapters.discord.client import DiscordClient
from bot_cmder.adapters.discord.schemas import Interaction, InteractionType, MessageCreatePayload
from bot_cmder.core.events import IncomingMessage, OutgoingResponse, Platform, PlatformUser

if TYPE_CHECKING:
    pass


# Discord renders @-mentions in message content as `<@USER_ID>` (or
# `<@!USER_ID>` for nicknames in older clients). Strip both shapes.
_MENTION_RE = re.compile(r"<@!?(\d+)>")


class DiscordAdapter(PlatformAdapter):
    platform = Platform.DISCORD

    def __init__(self, client: DiscordClient, bot_user_id: str | None = None) -> None:
        """`bot_user_id` is required for Gateway mode (so the adapter
        knows which mention IDs to strip from message content); for
        Interactions-only mode it can be None."""
        self._client = client
        self._bot_user_id = bot_user_id

    @property
    def client(self) -> DiscordClient:
        return self._client

    def parse(self, raw: Any) -> IncomingMessage | None:
        # Type-sniff between the two payload shapes. MessageCreatePayload
        # always has `content` + `author`; Interaction always has `type`
        # + `application_id`. Pre-validated objects pass through.
        if isinstance(raw, Interaction):
            return self._parse_interaction(raw)
        if isinstance(raw, MessageCreatePayload):
            return self._parse_message_create(raw)
        if isinstance(raw, dict):
            if "application_id" in raw and "type" in raw:
                return self._parse_interaction(Interaction.model_validate(raw))
            if "content" in raw and "author" in raw:
                return self._parse_message_create(MessageCreatePayload.model_validate(raw))
        return None

    def _parse_interaction(self, interaction: Interaction) -> IncomingMessage | None:
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

    def _parse_message_create(self, msg: MessageCreatePayload) -> IncomingMessage | None:
        """Decide whether a Gateway MESSAGE_CREATE counts as a command,
        and if so, normalize it to `/cmd args...` text shape.

        Three filters in order:
          1. Skip messages from any bot account (including ourselves) —
             prevents reply loops.
          2. Decide if the message addresses our bot:
               - DM (guild_id null): every message counts
               - guild channel: only if our bot is in `mentions`
          3. Strip the `<@bot_id>` mention prefix from content; if the
             remaining text doesn't already start with `/`, prepend it
             so the dispatcher recognizes it as a command.

        Returns None when any filter rejects (caller logs and moves on).
        """
        if msg.author.bot:
            return None
        # Guild messages must @-mention us to count as a command;
        # DMs accept any content.
        if not msg.is_dm() and (self._bot_user_id is None or not msg.mentions_user(self._bot_user_id)):
            return None

        # Strip every <@id> / <@!id> mention from the content (not just
        # the leading one) and collapse whitespace. This handles
        # `@sre_bot service restart` AND `service @sre_bot restart`
        # (the second is unusual but Discord allows mentions anywhere).
        bare = _MENTION_RE.sub(" ", msg.content).strip()
        bare = " ".join(bare.split())  # collapse runs of whitespace
        if not bare:
            return None

        text = bare if bare.startswith("/") else f"/{bare}"

        return IncomingMessage(
            platform=Platform.DISCORD,
            user=PlatformUser(
                platform=Platform.DISCORD,
                raw_id=msg.author.id,
                handle=msg.author.username,
                display_name=msg.author.global_name or msg.author.username,
            ),
            chat_id=msg.channel_id,
            text=text,
            message_id=msg.id,
            # Stash channel_id explicitly — adapter.send() uses it to
            # route the reply via the Gateway path (POST /channels/.../messages).
            raw={**msg.model_dump(), "_via": "gateway"},
            received_at=datetime.now(timezone.utc),
        )

    async def send(self, msg: IncomingMessage, resp: OutgoingResponse) -> None:
        # Branch by ingestion path. Gateway-originated messages
        # don't have a per-invocation interaction token — they reply
        # via the regular bot-authed REST endpoint.
        raw = msg.raw if isinstance(msg.raw, dict) else {}
        if raw.get("_via") == "gateway":
            await self._client.create_message(msg.chat_id, resp.text)
            return
        token = raw.get("token")
        if not token:
            # Without the per-interaction token we can't reach the
            # @original webhook URL; nothing to do.
            return
        await self._client.patch_original_response(token, resp.text)
