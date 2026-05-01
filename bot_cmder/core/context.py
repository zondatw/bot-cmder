from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from bot_cmder.core.events import IncomingMessage, Platform, PlatformUser

if TYPE_CHECKING:
    from bot_cmder.config.schema import AppConfig


@dataclass
class CommandContext:
    user: PlatformUser
    platform: Platform
    chat_id: str
    raw_event: dict[str, Any]
    config: AppConfig
    now: Callable[[], datetime]

    @classmethod
    def from_message(
        cls,
        msg: IncomingMessage,
        config: AppConfig,
        now: Callable[[], datetime] = datetime.now,
    ) -> CommandContext:
        return cls(
            user=msg.user,
            platform=msg.platform,
            chat_id=msg.chat_id,
            raw_event=msg.raw,
            config=config,
            now=now,
        )
