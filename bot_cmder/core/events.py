from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Platform(str, Enum):
    TELEGRAM = "telegram"
    DISCORD = "discord"
    SLACK = "slack"


@dataclass(frozen=True)
class PlatformUser:
    platform: Platform
    raw_id: str
    handle: str | None = None
    display_name: str | None = None

    @property
    def norm_id(self) -> str:
        return f"{self.platform.value}:{self.raw_id}"


@dataclass(frozen=True)
class IncomingMessage:
    platform: Platform
    user: PlatformUser
    chat_id: str
    text: str
    message_id: str | None
    raw: dict[str, Any]
    received_at: datetime


class ResponseKind(str, Enum):
    TEXT = "text"
    EPHEMERAL = "ephemeral"
    BUTTONS = "buttons"


@dataclass(frozen=True)
class Button:
    label: str
    value: str


@dataclass
class OutgoingResponse:
    kind: ResponseKind
    text: str
    buttons: list[list[Button]] = field(default_factory=list)
    reply_to_message_id: str | None = None

    @classmethod
    def text_reply(cls, text: str, reply_to: str | None = None) -> OutgoingResponse:
        return cls(kind=ResponseKind.TEXT, text=text, reply_to_message_id=reply_to)

    @classmethod
    def ephemeral(cls, text: str) -> OutgoingResponse:
        return cls(kind=ResponseKind.EPHEMERAL, text=text)
