from __future__ import annotations

import pytest

from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import OutgoingResponse
from bot_cmder.core.registry import Command, CommandRegistry, Risk, register


async def _noop(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
    return OutgoingResponse.text_reply("ok")


def test_register_and_lookup():
    reg = CommandRegistry()
    reg.register(Command(name="ping", risk=Risk.SAFE, handler=_noop))
    assert reg.get("ping") is not None
    assert reg.get("missing") is None


def test_duplicate_registration_raises():
    reg = CommandRegistry()
    reg.register(Command(name="ping", risk=Risk.SAFE, handler=_noop))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(Command(name="ping", risk=Risk.SAFE, handler=_noop))


def test_decorator_registers_to_passed_registry():
    reg = CommandRegistry()

    @register("greet", description="say hello", registry=reg)
    async def _h(ctx, args):
        return OutgoingResponse.text_reply("hi")

    cmd = reg.get("greet")
    assert cmd is not None
    assert cmd.description == "say hello"
    assert cmd.handler is _h


def test_effective_2fa_defaults_from_risk():
    safe = Command(name="a", risk=Risk.SAFE, handler=_noop)
    priv = Command(name="b", risk=Risk.PRIVILEGED, handler=_noop)
    forced_off = Command(name="c", risk=Risk.PRIVILEGED, handler=_noop, requires_2fa=False)
    forced_on = Command(name="d", risk=Risk.SAFE, handler=_noop, requires_2fa=True)

    assert safe.effective_2fa is False
    assert priv.effective_2fa is True
    assert forced_off.effective_2fa is False
    assert forced_on.effective_2fa is True
