from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyotp
import pytest
from cryptography.fernet import Fernet

from bot_cmder.audit.log import AuditLogger
from bot_cmder.auth.pending import PendingOTPSessions
from bot_cmder.auth.secret_store import SecretStore
from bot_cmder.auth.totp import TOTPVerifier
from bot_cmder.commands.builtin import otp
from bot_cmder.config.schema import AppConfig
from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import OutgoingResponse, Platform, PlatformUser
from bot_cmder.core.registry import Command, CommandRegistry, Risk


def _read_audit(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


@pytest.fixture
def setup(tmp_path: Path):
    """Returns (registry, pending, totp, audit, secret) with /otp + a target /restart installed."""
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLogger(audit_path)
    store = SecretStore(tmp_path / "totp.sqlite", Fernet.generate_key().decode())
    totp = TOTPVerifier(store)
    secret, _ = totp.enroll("telegram:111")

    pending = PendingOTPSessions(ttl_s=120)
    reg = CommandRegistry()

    # Target privileged command (no real OTP gate involved here — we
    # call its handler directly from /otp after verification).
    async def _restart(ctx, args):
        return OutgoingResponse.text_reply(f"restarted {args}")

    reg.register(Command(name="restart", risk=Risk.PRIVILEGED, handler=_restart))

    otp.install(reg, pending=pending, totp=totp, audit=audit)
    return reg, pending, totp, audit, audit_path, secret


def _ctx(config: AppConfig, *, user="telegram:111", chat="42", platform=Platform.TELEGRAM):
    return CommandContext(
        user=PlatformUser(platform=platform, raw_id=user.split(":")[1]),
        platform=platform,
        chat_id=chat,
        raw_event={},
        config=config,
        now=lambda: datetime.now(timezone.utc),
    )


async def test_no_pending_session_yields_explanation(setup, app_config, audit_path):
    reg, *_ = setup
    handler = reg.get("otp").handler
    resp = await handler(_ctx(app_config), ["123456"])
    assert "no pending" in resp.text
    assert _read_audit(audit_path)[0]["event"] == "OTP_NO_PENDING"


async def test_usage_when_args_missing_or_extra(setup, app_config):
    reg, *_ = setup
    handler = reg.get("otp").handler
    assert "usage" in (await handler(_ctx(app_config), [])).text
    assert "usage" in (await handler(_ctx(app_config), ["a", "b"])).text


async def test_cross_chat_submission_is_rejected(setup, app_config, audit_path):
    reg, pending, *_ = setup
    pending.stash(
        user_norm_id="telegram:111",
        chat_id="99",  # different from the /otp's chat
        platform=Platform.TELEGRAM,
        command_name="restart",
        args=["api"],
    )
    handler = reg.get("otp").handler
    resp = await handler(_ctx(app_config, chat="42"), ["123456"])
    assert "same chat" in resp.text
    assert _read_audit(audit_path)[0]["event"] == "OTP_CROSS_CHAT"


async def test_expired_session_is_rejected(setup, app_config, audit_path):
    reg, _, _, _, _, _ = setup
    # Build a pending session with a clock that has already moved past TTL.
    now = [datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)]
    expired_pending = PendingOTPSessions(ttl_s=10, clock=lambda: now[0])
    expired_pending.stash(
        user_norm_id="telegram:111",
        chat_id="42",
        platform=Platform.TELEGRAM,
        command_name="restart",
        args=["api"],
    )
    now[0] += timedelta(seconds=11)
    # Re-install /otp with this expired_pending so the handler sees it
    reg2 = CommandRegistry()
    audit2_path = audit_path.parent / "audit2.jsonl"
    audit2 = AuditLogger(audit2_path)
    otp.install(reg2, pending=expired_pending, totp=setup[2], audit=audit2)
    resp = await reg2.get("otp").handler(_ctx(app_config), ["123456"])
    assert "expired" in resp.text
    assert _read_audit(audit2_path)[0]["event"] == "OTP_EXPIRED"


async def test_invalid_code_is_rejected(setup, app_config, audit_path):
    reg, pending, _, _, _, _ = setup
    pending.stash(
        user_norm_id="telegram:111",
        chat_id="42",
        platform=Platform.TELEGRAM,
        command_name="restart",
        args=["api"],
    )
    resp = await reg.get("otp").handler(_ctx(app_config), ["000001"])  # wrong
    assert "invalid" in resp.text
    assert _read_audit(audit_path)[0]["event"] == "OTP_INVALID"


async def test_valid_code_runs_original_handler(setup, app_config, audit_path):
    reg, pending, _, _, _, secret = setup
    pending.stash(
        user_norm_id="telegram:111",
        chat_id="42",
        platform=Platform.TELEGRAM,
        command_name="restart",
        args=["api"],
    )
    code = pyotp.TOTP(secret).now()
    resp = await reg.get("otp").handler(_ctx(app_config), [code])
    assert "restarted ['api']" in resp.text
    events = _read_audit(audit_path)
    assert events[-1]["event"] == "EXECUTED"
    assert events[-1]["via_otp"] is True
    assert events[-1]["command"] == "restart"
    # The response carries the resumed command's metadata so platform
    # adapters (Slack visibility / chat echo) can render the actually-
    # executed command rather than the literal "/otp <code>" the user
    # typed. Risk goes to PRIVILEGED so by_risk → ephemeral keeps SSH
    # output private; displayed_command reconstructs the resumed call
    # from the synthetic name + stashed args.
    assert resp.risk == "privileged"
    assert resp.command_name == "restart"
    assert resp.displayed_command == "/restart api"


async def test_valid_code_resumes_command_with_no_args(setup, app_config, audit_path):
    """displayed_command for a no-args resumed command stays terse —
    no trailing space (a regression risk on the f-string reconstruction)."""
    reg, pending, _, _, _, secret = setup
    pending.stash(
        user_norm_id="telegram:111",
        chat_id="42",
        platform=Platform.TELEGRAM,
        command_name="restart",
        args=[],
    )
    code = pyotp.TOTP(secret).now()
    resp = await reg.get("otp").handler(_ctx(app_config), [code])
    assert resp.displayed_command == "/restart"


async def test_valid_code_when_command_was_unregistered_after_stash(setup, app_config, audit_path):
    reg, pending, _, _, _, secret = setup
    pending.stash(
        user_norm_id="telegram:111",
        chat_id="42",
        platform=Platform.TELEGRAM,
        command_name="ghost",  # not in registry
        args=[],
    )
    resp = await reg.get("otp").handler(_ctx(app_config), [pyotp.TOTP(secret).now()])
    assert "no longer registered" in resp.text
    assert _read_audit(audit_path)[0]["event"] == "OTP_COMMAND_GONE"


async def test_valid_code_resumes_router_subcommand(setup, app_config, audit_path):
    """Regression: /otp must resolve commands that live under a Router
    (e.g. `service_restart` inside the `service` router) and not just
    top-level Commands. Previously /otp called registry.get() which
    only sees the top-level dict, so the OTP would validate but the
    resume step printed "command no longer registered: /service_restart".
    """
    from bot_cmder.core.registry import Risk

    reg, pending, _, _, _, secret = setup

    router = reg.create_router("svc", description="x")
    captured = {}

    @router.subcommand("restart", risk=Risk.PRIVILEGED, description="restart")
    async def _r(ctx, args):
        captured["args"] = args
        return OutgoingResponse.text_reply("router restarted")

    # Dispatcher would stash under the synthetic name `svc_restart`.
    pending.stash(
        user_norm_id="telegram:111",
        chat_id="42",
        platform=Platform.TELEGRAM,
        command_name="svc_restart",
        args=["api"],
    )
    resp = await reg.get("otp").handler(_ctx(app_config), [pyotp.TOTP(secret).now()])
    assert "router restarted" in resp.text
    assert captured["args"] == ["api"]
    audited = _read_audit(audit_path)[-1]
    assert audited["event"] == "EXECUTED"
    assert audited["command"] == "svc_restart"
    assert audited["via_otp"] is True
