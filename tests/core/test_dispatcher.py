from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bot_cmder.auth.acl import check_allowed
from bot_cmder.core.dispatcher import Dispatcher
from bot_cmder.core.events import OutgoingResponse
from bot_cmder.core.registry import Command, CommandRegistry, Risk


async def _ok_handler(ctx, args):
    return OutgoingResponse.text_reply(f"args={args}")


async def _slow_handler(ctx, args):
    await asyncio.sleep(2)
    return OutgoingResponse.text_reply("done")


async def _boom_handler(ctx, args):
    raise RuntimeError("boom")


def _read_audit(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


@pytest.fixture
def registry() -> CommandRegistry:
    reg = CommandRegistry()
    reg.register(Command(name="ping", risk=Risk.SAFE, handler=_ok_handler))
    reg.register(Command(name="slow", risk=Risk.SAFE, handler=_slow_handler, timeout_s=1))
    reg.register(Command(name="boom", risk=Risk.SAFE, handler=_boom_handler))
    reg.register(Command(name="restart", risk=Risk.PRIVILEGED, handler=_ok_handler))
    return reg


@pytest.fixture
def dispatcher(registry, app_config, audit) -> Dispatcher:
    return Dispatcher(
        registry=registry,
        config=app_config,
        audit=audit,
        acl_check=check_allowed,
    )


async def test_non_command_text_is_ignored(dispatcher, make_message):
    resp = await dispatcher.dispatch(make_message("hello world"))
    assert resp is None


async def test_unknown_command_is_audited(dispatcher, make_message, audit_path):
    resp = await dispatcher.dispatch(make_message("/nope"))
    assert resp is not None and "unknown" in resp.text
    events = _read_audit(audit_path)
    assert events[0]["event"] == "COMMAND_UNKNOWN"


async def test_safe_command_runs_for_whitelisted_role(dispatcher, make_message, audit_path):
    resp = await dispatcher.dispatch(make_message("/ping foo bar"))
    assert resp is not None and "args=" in resp.text
    events = _read_audit(audit_path)
    assert events[0]["event"] == "EXECUTED"
    assert events[0]["command"] == "ping"


async def test_privileged_command_denied_without_acl(dispatcher, make_message, audit_path):
    # 'restart' allows role:sre; user 'telegram:222' is viewer, not sre
    resp = await dispatcher.dispatch(make_message("/restart api", user_id="222"))
    assert resp is not None and "forbidden" in resp.text
    events = _read_audit(audit_path)
    assert events[0]["event"] == "AUTH_DENIED"


async def test_privileged_command_allowed_for_sre(dispatcher, make_message, audit_path):
    resp = await dispatcher.dispatch(make_message("/restart api", user_id="111"))
    assert resp is not None and "args=['api']" in resp.text
    events = _read_audit(audit_path)
    assert events[0]["event"] == "EXECUTED"


async def test_handler_timeout_is_caught(dispatcher, make_message, audit_path):
    resp = await dispatcher.dispatch(make_message("/slow"))
    assert resp is not None and "timeout" in resp.text
    events = _read_audit(audit_path)
    assert events[0]["event"] == "COMMAND_TIMEOUT"


async def test_handler_exception_is_caught(dispatcher, make_message, audit_path):
    resp = await dispatcher.dispatch(make_message("/boom"))
    assert resp is not None and "error" in resp.text
    events = _read_audit(audit_path)
    assert events[0]["event"] == "HANDLER_ERROR"
    assert "boom" in events[0]["error"]


async def test_audit_records_norm_id_and_chat(dispatcher, make_message, audit_path: Path):
    await dispatcher.dispatch(make_message("/ping", user_id="111", chat_id="999"))
    events = _read_audit(audit_path)
    assert events[0]["user"] == "telegram:111"
    assert events[0]["chat"] == "999"


# --- Phase 2: OTP gate ---------------------------------------------------


@pytest.fixture
def pending():
    from bot_cmder.auth.pending import PendingOTPSessions

    return PendingOTPSessions(ttl_s=120)


@pytest.fixture
def gated_dispatcher(registry, app_config, audit, pending) -> Dispatcher:
    return Dispatcher(
        registry=registry,
        config=app_config,
        audit=audit,
        acl_check=check_allowed,
        pending=pending,
    )


async def test_privileged_command_with_gate_stashes_pending_session(
    gated_dispatcher, pending, make_message, audit_path
):
    resp = await gated_dispatcher.dispatch(make_message("/restart api", user_id="111"))
    assert resp is not None
    assert "Privileged" in resp.text and "/otp" in resp.text
    # Session was stashed for this user
    session = pending.pop("telegram:111")
    assert session is not None
    assert session.command_name == "restart"
    assert session.args == ["api"]
    # Audit shows OTP_REQUESTED, not EXECUTED
    events = _read_audit(audit_path)
    assert events[0]["event"] == "OTP_REQUESTED"
    assert events[0]["command"] == "restart"


async def test_safe_command_bypasses_gate_even_when_pending_set(gated_dispatcher, pending, make_message, audit_path):
    resp = await gated_dispatcher.dispatch(make_message("/ping", user_id="111"))
    assert resp is not None and "args=" in resp.text
    events = _read_audit(audit_path)
    assert events[0]["event"] == "EXECUTED"
    # No session was stashed for SAFE commands
    assert pending.pop("telegram:111") is None


async def test_privileged_command_without_gate_runs_inline_phase1_behavior(dispatcher, make_message, audit_path):
    # Reuses the original `dispatcher` fixture which was built with
    # pending=None (Phase 1 wiring). The bypass keeps backward
    # compatibility for tests / deployments without TOTP.
    resp = await dispatcher.dispatch(make_message("/restart api", user_id="111"))
    events = _read_audit(audit_path)
    assert events[0]["event"] == "EXECUTED"
    assert "args=['api']" in resp.text
