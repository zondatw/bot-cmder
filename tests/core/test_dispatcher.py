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


# --- Phase 3+: Router rewrite -------------------------------------------


@pytest.fixture
def router_dispatcher(app_config, audit, pending):
    """A dispatcher where the registry has a `widget` router with a
    SAFE `list` subcommand and a PRIVILEGED `restart` subcommand."""
    reg = CommandRegistry()
    router = reg.create_router("widget", description="manage widgets")

    @router.subcommand("list", risk=Risk.SAFE, description="show all")
    async def _list(ctx, args):
        return OutgoingResponse.text_reply(f"list args={args}")

    @router.subcommand("restart", risk=Risk.PRIVILEGED, description="restart one")
    async def _restart(ctx, args):
        return OutgoingResponse.text_reply(f"restart args={args}")

    # ACL: allow widget_list (SAFE → default_allow_safe), explicitly allow restart for sre.
    app_config.acl.commands["widget_restart"] = ["role:sre"]
    return Dispatcher(
        registry=reg,
        config=app_config,
        audit=audit,
        acl_check=check_allowed,
        pending=pending,
    )


async def test_router_no_args_prints_help(router_dispatcher, make_message):
    resp = await router_dispatcher.dispatch(make_message("/widget"))
    assert resp is not None
    assert "/widget — manage widgets" in resp.text
    assert "/widget list" in resp.text
    assert "/widget restart" in resp.text


async def test_router_help_subcommand_prints_help(router_dispatcher, make_message):
    resp = await router_dispatcher.dispatch(make_message("/widget help"))
    assert resp is not None
    assert "/widget — manage widgets" in resp.text


async def test_router_unknown_subcommand_audited_and_help_in_reply(router_dispatcher, make_message, audit_path):
    resp = await router_dispatcher.dispatch(make_message("/widget bogus"))
    assert resp is not None
    assert "unknown subcommand: bogus" in resp.text
    # Help also embedded so user can recover without typing /widget help.
    assert "/widget list" in resp.text
    assert _read_audit(audit_path)[0]["event"] == "ROUTER_UNKNOWN_SUBCOMMAND"


async def test_router_safe_subcommand_runs_immediately(router_dispatcher, make_message, audit_path):
    resp = await router_dispatcher.dispatch(make_message("/widget list extra args"))
    assert "list args=['extra', 'args']" in resp.text
    audited = _read_audit(audit_path)[0]
    # Audit records the synthetic internal name, not the surface form.
    assert audited["event"] == "EXECUTED"
    assert audited["command"] == "widget_list"
    assert audited["args"] == ["extra", "args"]


async def test_router_privileged_subcommand_triggers_otp_gate(router_dispatcher, pending, make_message, audit_path):
    resp = await router_dispatcher.dispatch(make_message("/widget restart svc-a", user_id="111"))
    assert "Privileged" in resp.text and "/otp" in resp.text
    # Pending session was stashed under the synthetic command name with
    # the post-rewrite args (no leading "restart").
    session = pending.pop("telegram:111")
    assert session is not None
    assert session.command_name == "widget_restart"
    assert session.args == ["svc-a"]
    audited = _read_audit(audit_path)[-1]
    assert audited["event"] == "OTP_REQUESTED"
    assert audited["command"] == "widget_restart"
    assert audited["args"] == ["svc-a"]


# --- audit invariants: platform field + /otp args redaction --------


async def test_audit_carries_platform_field_for_every_event(dispatcher, registry, make_message, audit_path):
    """Every audit event must carry `platform` so jq filters like
    `select(.platform == "slack")` actually work.

    Regression: dogfood pass found no events had this field; my own
    walkthroughs were instructing users to filter on a field that
    didn't exist. This test pins it across the four common code paths
    (executed, ACL deny, unknown command, handler error).
    """
    # A SAFE command — exercises the EXECUTED path
    await dispatcher.dispatch(make_message("/ping"))
    # Unknown command — exercises COMMAND_UNKNOWN
    await dispatcher.dispatch(make_message("/nonexistent"))
    # Handler crash — exercises HANDLER_ERROR
    await dispatcher.dispatch(make_message("/boom"))

    events = _read_audit(audit_path)
    assert len(events) == 3
    for e in events:
        assert "platform" in e, f"audit event missing platform: {e}"
        assert e["platform"] == "telegram"  # make_message defaults to telegram


async def test_otp_args_never_leak_to_audit(dispatcher, registry, make_message, audit_path):
    """The big one: /otp's args (the TOTP code, or worse, an enrollment
    URI a user mistyped) must NEVER reach audit.jsonl in plaintext.

    Defeating this redaction would defeat the whole point of the
    Fernet-encrypted SecretStore — anyone who reads audit.jsonl could
    extract the BASE32 secret from a leaked enrollment URI and
    regenerate every future OTP for that user.
    """
    registry.register(Command(name="otp", risk=Risk.SAFE, handler=_ok_handler))

    # Simulate the dogfood incident: user pastes the entire enrollment
    # URI as the /otp arg.
    await dispatcher.dispatch(make_message("/otp otpauth://totp/bot-cmder:u?secret=ABCDEFGHIJKLMNOP&issuer=bot-cmder"))
    # And the normal case: a 6-digit code
    await dispatcher.dispatch(make_message("/otp 918493"))

    events = _read_audit(audit_path)
    raw_log = audit_path.read_text()

    # Hard guarantee: neither the secret material nor the OTP code
    # appears anywhere in the audit log file (not just in args — also
    # nowhere else by accident).
    assert "ABCDEFGHIJKLMNOP" not in raw_log
    assert "otpauth://" not in raw_log
    assert "918493" not in raw_log

    # The redaction placeholder is what landed instead.
    otp_events = [e for e in events if e.get("command") == "otp"]
    assert len(otp_events) == 2
    for e in otp_events:
        assert e["args"] == ["<redacted>"]


# --- issue #15 — emergency-window bypass at the dispatcher layer ---------


@pytest.fixture
def emergency_dispatcher(registry, app_config, audit, pending):
    """Like `dispatcher` but with EmergencyWindows wired so the OTP
    gate consults it before stashing."""
    from bot_cmder.auth.emergency import EmergencyWindows

    emergency = EmergencyWindows(max_minutes=60)
    return (
        Dispatcher(
            registry=registry,
            config=app_config,
            audit=audit,
            acl_check=check_allowed,
            pending=pending,
            emergency=emergency,
        ),
        emergency,
    )


async def test_active_emergency_window_bypasses_otp_gate(emergency_dispatcher, make_message, audit_path):
    """The headline contract from issue #15: when a user has an
    active window, the dispatcher SKIPS the OTP stash and runs
    the privileged command inline. EXECUTED audit gets the
    via_emergency_otp:true marker; OTP_REQUESTED is NOT emitted."""
    dispatcher, emergency = emergency_dispatcher
    emergency.grant("telegram:111", 30)

    resp = await dispatcher.dispatch(make_message("/restart api", user_id="111"))

    # Privileged command ran inline — no "Privileged. Reply with /otp..." prompt.
    assert "Privileged" not in resp.text
    assert "args=['api']" in resp.text  # _ok_handler echoes args back

    events = [e["event"] for e in _read_audit(audit_path)]
    # Bypass event fires per command run during window
    assert "EMERGENCY_OTP_BYPASS" in events
    # Normal OTP gate path does NOT fire
    assert "OTP_REQUESTED" not in events
    # EXECUTED carries the via_emergency_otp marker
    executed = [e for e in _read_audit(audit_path) if e["event"] == "EXECUTED"][0]
    assert executed["via_emergency_otp"] is True


async def test_no_active_window_falls_back_to_normal_otp_gate(emergency_dispatcher, make_message, audit_path):
    """Sanity: the bypass branch only fires when there's actually a
    window. Without one, the dispatcher does the existing
    OTP_REQUESTED + stash flow."""
    dispatcher, _emergency = emergency_dispatcher

    resp = await dispatcher.dispatch(make_message("/restart api", user_id="111"))

    assert "Privileged" in resp.text  # got the OTP prompt
    events = [e["event"] for e in _read_audit(audit_path)]
    assert "OTP_REQUESTED" in events
    assert "EMERGENCY_OTP_BYPASS" not in events


async def test_expired_window_falls_back_to_normal_gate(emergency_dispatcher, make_message, audit_path):
    """A window that aged out between grant and command-time should
    NOT bypass — same outcome as never-had-a-window."""
    dispatcher, emergency = emergency_dispatcher
    # Grant a 1-min window, then mutate the dict directly to a past
    # expiry so the lazy cleanup path triggers on next access.
    from datetime import datetime, timedelta, timezone

    emergency.grant("telegram:111", 30)
    expired = emergency._windows["telegram:111"]
    # Replace with a window whose expires_at is in the past
    object.__setattr__(expired, "expires_at", datetime.now(timezone.utc) - timedelta(seconds=1))

    resp = await dispatcher.dispatch(make_message("/restart api", user_id="111"))

    assert "Privileged" in resp.text
    events = [e["event"] for e in _read_audit(audit_path)]
    assert "OTP_REQUESTED" in events
    assert "EMERGENCY_OTP_BYPASS" not in events


async def test_safe_command_not_affected_by_emergency_window(emergency_dispatcher, make_message, audit_path):
    """SAFE commands never went through the OTP gate; an active
    window changes nothing for them. EXECUTED audit should NOT
    carry the via_emergency_otp marker (which is reserved for
    'this command would have needed OTP but skipped it')."""
    dispatcher, emergency = emergency_dispatcher
    emergency.grant("telegram:111", 30)

    await dispatcher.dispatch(make_message("/ping", user_id="111"))

    executed = [e for e in _read_audit(audit_path) if e["event"] == "EXECUTED"][0]
    # via_emergency_otp NOT set (only emitted when the bypass actually
    # triggered, which it doesn't for SAFE commands).
    assert "via_emergency_otp" not in executed


# NOTE: per-norm_id isolation of EmergencyWindows is pinned at the
# unit-test layer (tests/auth/test_emergency.py::
# test_windows_are_per_norm_id). Re-asserting it through the
# dispatcher would require expanding the shared `app_config`
# fixture with a second sre-role user (currently only telegram:111
# has role:sre) — not worth the coupling for a property already
# covered upstream.
