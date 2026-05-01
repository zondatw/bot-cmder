from __future__ import annotations

from datetime import timezone

from bot_cmder.adapters.telegram.adapter import TelegramAdapter
from bot_cmder.adapters.telegram.client import TelegramClient
from bot_cmder.adapters.telegram.schemas import TelegramUpdate
from bot_cmder.core.events import Platform


def _update(text: str = "/health", chat_id: int = 42, user_id: int = 99) -> TelegramUpdate:
    return TelegramUpdate.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 5,
                "date": 1700000000,
                "chat": {"id": chat_id, "type": "private", "first_name": "X"},
                "from": {"id": user_id, "is_bot": False, "first_name": "Zonda", "username": "zondatw"},
                "text": text,
                "entities": [{"offset": 0, "length": len(text), "type": "bot_command"}],
            },
        }
    )


def test_parse_produces_normalized_incoming_message():
    adapter = TelegramAdapter(TelegramClient(token="fake"))
    msg = adapter.parse(_update())
    assert msg is not None
    assert msg.platform == Platform.TELEGRAM
    assert msg.user.norm_id == "telegram:99"
    assert msg.user.handle == "zondatw"
    assert msg.user.display_name == "Zonda"
    assert msg.chat_id == "42"
    assert msg.text == "/health"
    assert msg.message_id == "5"
    assert msg.received_at.tzinfo == timezone.utc


def test_parse_returns_none_on_non_text_message():
    adapter = TelegramAdapter(TelegramClient(token="fake"))
    update = TelegramUpdate.model_validate({"update_id": 1})
    assert adapter.parse(update) is None


def test_parse_handles_user_without_username_or_lastname():
    """Group-chat users sometimes omit username; bot accounts often omit
    last_name. The legacy serializer required these to be set, which
    crashed on real-world payloads."""
    adapter = TelegramAdapter(TelegramClient(token="fake"))
    update = TelegramUpdate.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 5,
                "date": 1700000000,
                "chat": {"id": 42, "type": "group", "title": "ops"},
                "from": {"id": 99, "is_bot": False, "first_name": "Z"},
                "text": "/health",
            },
        }
    )
    msg = adapter.parse(update)
    assert msg is not None
    assert msg.user.handle is None
