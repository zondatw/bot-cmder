"""Pydantic models for Discord HTTP Interactions payloads.

We model only what we actually consume:

  - The top-level Interaction envelope (id, application_id, type,
    token, data, user / member, channel_id).
  - Application-command data (name + flat list of options).
  - Option values as strings (we map every option type 3 to str
    and ignore other types — slash command schema is fully
    controlled by us, so this is safe).

PING (type=1) interactions have an empty `data`; only the type
matters there. We tolerate the absence of `data` rather than
modelling a separate envelope.
"""

from __future__ import annotations

from enum import IntEnum

from pydantic import BaseModel


class InteractionType(IntEnum):
    PING = 1
    APPLICATION_COMMAND = 2
    MESSAGE_COMPONENT = 3
    APPLICATION_COMMAND_AUTOCOMPLETE = 4
    MODAL_SUBMIT = 5


class InteractionResponseType(IntEnum):
    PONG = 1
    CHANNEL_MESSAGE_WITH_SOURCE = 4
    DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE = 5
    DEFERRED_UPDATE_MESSAGE = 6


class DiscordUser(BaseModel):
    id: str
    username: str | None = None
    global_name: str | None = None
    # Phase 6c — `bot=True` on a MESSAGE_CREATE author lets the
    # Gateway daemon skip messages from other bots AND from itself
    # without dispatching them. Filters apply to gateway flow only;
    # interactions can never come from a bot account.
    bot: bool = False


class DiscordMember(BaseModel):
    user: DiscordUser | None = None


class InteractionDataOption(BaseModel):
    """One field of an application-command invocation.

    Two shapes occur:

      - Leaf option (STRING / INTEGER / etc.): has `value`, no `options`.
        e.g. `{"name": "args", "type": 3, "value": "restart api"}`
      - SUB_COMMAND (`type=1`) or SUB_COMMAND_GROUP (`type=2`):
        no `value`, has nested `options` carrying the actual leaf
        values. Used by /otp's per-syntax schema (`/otp emergency 5`,
        `/otp end`, `/otp status`, `/otp code 123456`) — see
        `scripts/register_discord_commands.py` and issue #18.

    The DiscordAdapter walks the tree once and flattens it back to
    the same `/cmd args...` text shape Telegram produces.
    """

    name: str
    type: int
    value: str | int | float | bool | None = None
    options: list[InteractionDataOption] | None = None


class InteractionData(BaseModel):
    id: str | None = None
    name: str | None = None
    type: int | None = None
    options: list[InteractionDataOption] | None = None


class Interaction(BaseModel):
    id: str
    application_id: str
    type: InteractionType
    token: str
    data: InteractionData | None = None
    guild_id: str | None = None
    channel_id: str | None = None
    user: DiscordUser | None = None  # populated in DMs
    member: DiscordMember | None = None  # populated in guild channels
    version: int = 1

    def caller(self) -> DiscordUser | None:
        """Whichever of `user` / `member.user` holds the invoker."""
        if self.user is not None:
            return self.user
        if self.member is not None and self.member.user is not None:
            return self.member.user
        return None


# --- Phase 6c — Gateway MESSAGE_CREATE event payload ----------------
#
# Slash commands in the Phase 4 webhook flow always come in as the
# Interaction envelope above. Phase 6c's Gateway path is different:
# Discord pushes raw chat messages over the WSS, packaged as
# `MESSAGE_CREATE` dispatch events with a payload shape modelled here.
#
# We only need a small subset of fields to decide "is this command-
# shaped, and who issued it?":
#   - `content` — the literal text the user typed (requires the
#     MESSAGE_CONTENT privileged intent to be enabled in the Discord
#     dev portal; otherwise this field arrives empty)
#   - `author` — same DiscordUser shape used in interactions
#   - `channel_id` — needed to reply via REST
#   - `guild_id` — null for DMs (which is significant: DMs accept
#     bare commands without an @-mention prefix)
#   - `mentions` — used to detect "this message @-mentions our bot"
#     in guild channels (DMs always count as command context)
#
# Many other fields (timestamp, embeds, attachments, ...) come down
# the wire but aren't relevant to the SRE-bot use case; pydantic's
# `extra='ignore'` drops them silently so a Discord schema bump won't
# break us.


class MessageMention(BaseModel):
    id: str  # the mentioned user's snowflake ID


class MessageCreatePayload(BaseModel):
    """Subset of Discord's MESSAGE_CREATE event payload."""

    id: str
    content: str = ""  # empty when MESSAGE_CONTENT intent is missing
    channel_id: str
    guild_id: str | None = None  # null = DM
    author: DiscordUser
    mentions: list[MessageMention] = []
    # `bot` flag on the author lets us skip messages from other bots
    # (and our own messages) without dispatching them — without this
    # filter, replies could trigger an infinite loop.

    def is_dm(self) -> bool:
        return self.guild_id is None

    def mentions_user(self, user_id: str) -> bool:
        return any(m.id == user_id for m in self.mentions)
