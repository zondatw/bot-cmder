from __future__ import annotations

from datetime import timezone

import pytest

from bot_cmder.adapters.discord.adapter import DiscordAdapter
from bot_cmder.adapters.discord.client import DiscordClient
from bot_cmder.core.events import Platform


@pytest.fixture
def adapter():
    return DiscordAdapter(DiscordClient(bot_token="fake", application_id="123"))


def test_parse_application_command_with_args_option(adapter):
    payload = {
        "id": "1",
        "application_id": "2",
        "type": 2,
        "token": "tok",
        "version": 1,
        "channel_id": "42",
        "user": {"id": "9", "username": "zondatw", "global_name": "Zonda Y"},
        "data": {
            "id": "cmd",
            "name": "service",
            "type": 1,
            "options": [{"name": "args", "type": 3, "value": "restart api --host gce"}],
        },
    }
    msg = adapter.parse(payload)
    assert msg.platform == Platform.DISCORD
    assert msg.user.norm_id == "discord:9"
    assert msg.user.handle == "zondatw"
    assert msg.user.display_name == "Zonda Y"
    assert msg.text == "/service restart api --host gce"
    assert msg.chat_id == "42"
    assert msg.message_id == "1"
    assert msg.received_at.tzinfo == timezone.utc


def test_parse_uses_member_user_when_in_guild(adapter):
    payload = {
        "id": "1",
        "application_id": "2",
        "type": 2,
        "token": "tok",
        "version": 1,
        "channel_id": "42",
        "guild_id": "g1",
        "member": {"user": {"id": "9", "username": "zondatw"}},
        "data": {"name": "help", "type": 1},
    }
    msg = adapter.parse(payload)
    assert msg.user.norm_id == "discord:9"
    assert msg.text == "/help"


def test_parse_concatenates_options_in_order(adapter):
    """If a slash command somehow ships with multiple STRING options
    (e.g. a future schema change adds a `flags` option after `args`),
    they should be space-joined in the order Discord sent them."""
    payload = {
        "id": "1",
        "application_id": "2",
        "type": 2,
        "token": "tok",
        "version": 1,
        "channel_id": "42",
        "user": {"id": "9", "username": "z"},
        "data": {
            "name": "kubectl",
            "type": 1,
            "options": [
                {"name": "args", "type": 3, "value": "get"},
                {"name": "args2", "type": 3, "value": "pods"},
            ],
        },
    }
    msg = adapter.parse(payload)
    assert msg.text == "/kubectl get pods"


def test_parse_ping_returns_none(adapter):
    payload = {"id": "1", "application_id": "2", "type": 1, "token": "t", "version": 1}
    assert adapter.parse(payload) is None


def test_parse_command_without_caller_returns_none(adapter):
    """Defensive: if neither `user` nor `member.user` is populated
    (shouldn't happen in real Discord traffic but the schema allows
    it), refuse to build a half-formed IncomingMessage."""
    payload = {
        "id": "1",
        "application_id": "2",
        "type": 2,
        "token": "t",
        "version": 1,
        "channel_id": "42",
        "data": {"name": "help", "type": 1},
    }
    assert adapter.parse(payload) is None


def test_parse_falls_back_to_dm_chat_id_when_no_channel(adapter):
    payload = {
        "id": "1",
        "application_id": "2",
        "type": 2,
        "token": "t",
        "version": 1,
        "user": {"id": "9", "username": "z"},
        "data": {"name": "help", "type": 1},
    }
    msg = adapter.parse(payload)
    assert msg.chat_id == "dm"
