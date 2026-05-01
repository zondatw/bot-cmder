"""Unit tests for the Router abstraction in core/registry.py.

Dispatcher-side router rewrite is exercised in test_dispatcher.py;
this file pins Router's own contracts in isolation.
"""

from __future__ import annotations

import pytest

from bot_cmder.core.events import OutgoingResponse
from bot_cmder.core.registry import CommandRegistry, Risk, Router


async def _ok(ctx, args):
    return OutgoingResponse.text_reply("ok")


def test_create_router_registers_and_returns_it():
    reg = CommandRegistry()
    r = reg.create_router("svc", description="x")
    assert reg.get_router("svc") is r
    assert reg.all_routers() == [r]


def test_subcommand_creates_internal_command_with_synthetic_name():
    reg = CommandRegistry()
    r = reg.create_router("svc")

    @r.subcommand("restart", risk=Risk.PRIVILEGED, description="restart it", timeout_s=99)
    async def _h(ctx, args):
        return OutgoingResponse.text_reply("done")

    cmd = r.get_subcommand("restart")
    assert cmd is not None
    assert cmd.name == "svc_restart"  # <router>_<sub> synthetic name
    assert cmd.risk == Risk.PRIVILEGED
    assert cmd.description == "restart it"
    assert cmd.timeout_s == 99
    assert cmd.effective_2fa is True


def test_duplicate_subcommand_raises():
    reg = CommandRegistry()
    r = reg.create_router("svc")

    @r.subcommand("a")
    async def _a(ctx, args):
        return OutgoingResponse.text_reply("a")

    with pytest.raises(ValueError, match="already registered"):

        @r.subcommand("a")
        async def _a2(ctx, args):
            return OutgoingResponse.text_reply("a2")


def test_router_subcommand_does_not_appear_in_top_level_commands():
    """Subcommands belong to the router, not to registry.all() —
    otherwise /help and Telegram menu would double-count."""
    reg = CommandRegistry()
    r = reg.create_router("svc")

    @r.subcommand("restart")
    async def _h(ctx, args):
        return OutgoingResponse.text_reply("ok")

    assert reg.all() == []
    assert reg.get("svc_restart") is None  # only via the router
    flattened = {c.name for c in reg.all_executable_commands()}
    assert flattened == {"svc_restart"}


def test_router_name_clashes_with_existing_command_raises():
    from bot_cmder.core.registry import Command

    reg = CommandRegistry()
    reg.register(Command(name="svc", risk=Risk.SAFE, handler=_ok))
    with pytest.raises(ValueError, match="clashes"):
        reg.create_router("svc")


def test_command_name_clashes_with_existing_router_raises():
    from bot_cmder.core.registry import Command

    reg = CommandRegistry()
    reg.create_router("svc")
    with pytest.raises(ValueError, match="clashes"):
        reg.register(Command(name="svc", risk=Risk.SAFE, handler=_ok))


def test_subcommand_names_are_sorted():
    reg = CommandRegistry()
    r = reg.create_router("svc")
    for n in ("zebra", "alpha", "mike"):

        @r.subcommand(n)
        async def _h(ctx, args, _name=n):
            return OutgoingResponse.text_reply(_name)

    assert r.subcommand_names() == ["alpha", "mike", "zebra"]


def test_help_text_lists_subcommands_with_risk_tag_and_description():
    reg = CommandRegistry()
    r = reg.create_router("svc", description="manage services")

    @r.subcommand("list", risk=Risk.SAFE, description="show all")
    async def _l(ctx, args):
        return OutgoingResponse.text_reply("ok")

    @r.subcommand("restart", risk=Risk.PRIVILEGED, description="restart one")
    async def _r(ctx, args):
        return OutgoingResponse.text_reply("ok")

    text = r.help_text()
    assert "/svc — manage services" in text
    assert "/svc list" in text
    assert "show all" in text
    assert "/svc restart" in text
    assert "[privileged]" in text
    assert "restart one" in text


def test_help_text_when_no_subcommands_registered():
    r = Router(name="svc", description="empty")
    text = r.help_text()
    assert "no subcommands" in text
