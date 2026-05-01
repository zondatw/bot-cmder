from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bot_cmder.adapters.base import PlatformAdapter
from bot_cmder.adapters.telegram.client import TelegramClient
from bot_cmder.adapters.telegram.schemas import TelegramUpdate
from bot_cmder.core.events import IncomingMessage, OutgoingResponse, Platform, PlatformUser


class TelegramAdapter(PlatformAdapter):
    platform = Platform.TELEGRAM

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    @property
    def client(self) -> TelegramClient:
        return self._client

    def parse(self, raw: Any) -> IncomingMessage | None:
        update = raw if isinstance(raw, TelegramUpdate) else TelegramUpdate.model_validate(raw)
        msg = update.message
        if msg is None or msg.text is None or msg.from_user is None:
            return None

        u = msg.from_user
        display = " ".join(filter(None, [u.first_name, u.last_name])) or None
        user = PlatformUser(
            platform=Platform.TELEGRAM,
            raw_id=str(u.id),
            handle=u.username,
            display_name=display,
        )
        return IncomingMessage(
            platform=Platform.TELEGRAM,
            user=user,
            chat_id=str(msg.chat.id),
            text=msg.text,
            message_id=str(msg.message_id),
            raw=update.model_dump(by_alias=True),
            received_at=datetime.fromtimestamp(msg.date, tz=timezone.utc),
        )

    async def send(self, msg: IncomingMessage, resp: OutgoingResponse) -> None:
        if not resp.text:
            return
        reply_to = int(resp.reply_to_message_id) if resp.reply_to_message_id else None
        await self._client.send_message(
            chat_id=int(msg.chat_id),
            text=resp.text,
            reply_to_message_id=reply_to,
        )
