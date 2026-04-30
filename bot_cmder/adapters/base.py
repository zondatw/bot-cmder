from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from bot_cmder.core.events import IncomingMessage, OutgoingResponse, Platform


class PlatformAdapter(ABC):
    """How a chat platform talks to bot-cmder.

    Each adapter owns its own incoming-payload schema and outbound
    client; the only contract here is the conversion in/out of the
    platform-neutral IncomingMessage / OutgoingResponse types.
    """

    platform: Platform

    @abstractmethod
    def parse(self, raw: Any) -> IncomingMessage | None: ...

    @abstractmethod
    async def send(self, msg: IncomingMessage, resp: OutgoingResponse) -> None: ...
