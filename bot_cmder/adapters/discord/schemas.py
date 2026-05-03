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


class DiscordMember(BaseModel):
    user: DiscordUser | None = None


class InteractionDataOption(BaseModel):
    """One field of an application-command invocation.

    For the MVP slash command schema (one `args` string option per
    top-level command, plus `code` for /otp) every option we care
    about has type 3 (STRING) and a `value` string. Sub-command
    options (type 1/2) are not produced by our schema, so we don't
    decode them here.
    """

    name: str
    type: int
    value: str | int | float | bool | None = None


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
