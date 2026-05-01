from __future__ import annotations

from datetime import datetime, timezone

from bot_cmder.commands.builtin.whoami import install
from bot_cmder.config.schema import AppConfig, UserConfig
from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import Platform, PlatformUser
from bot_cmder.core.registry import CommandRegistry


def _ctx(user_id: str, config: AppConfig, *, handle: str | None = "zondatw") -> CommandContext:
    return CommandContext(
        user=PlatformUser(platform=Platform.TELEGRAM, raw_id=user_id, handle=handle, display_name="Zonda Y."),
        platform=Platform.TELEGRAM,
        chat_id="42",
        raw_event={},
        config=config,
        now=lambda: datetime.now(timezone.utc),
    )


def _handler(reg: CommandRegistry):
    return reg.get("whoami").handler


async def test_known_user_shows_role_and_logical_id():
    cfg = AppConfig(users=[UserConfig(id="zonda", aliases=["telegram:111"], role="sre")])
    reg = CommandRegistry()
    install(reg)
    resp = await _handler(reg)(_ctx("111", cfg), [])
    assert "norm_id: telegram:111" in resp.text
    assert "id: zonda" in resp.text
    assert "role: sre" in resp.text
    assert "handle: zondatw" in resp.text


async def test_unknown_user_marked_as_not_in_config():
    cfg = AppConfig()
    reg = CommandRegistry()
    install(reg)
    resp = await _handler(reg)(_ctx("999", cfg, handle=None), [])
    assert "norm_id: telegram:999" in resp.text
    assert "id: <not in config>" in resp.text
    assert "role: <none>" in resp.text
    assert "handle: -" in resp.text
