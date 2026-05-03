"""Tests for SlackAdapter.

Two halves:

  - parse() — Slack form payload → IncomingMessage with the right
    text shape (so the existing dispatcher + router rewrite work
    unchanged).
  - send() — apply visibility rules from SlackConfig (the user's
    explicit ask: "by_risk" + per-command overrides + global modes).
"""

from __future__ import annotations

import pytest

from bot_cmder.adapters.slack.adapter import SlackAdapter
from bot_cmder.adapters.slack.client import SlackClient
from bot_cmder.adapters.slack.schemas import SlashCommandPayload
from bot_cmder.config.schema import SlackConfig
from bot_cmder.core.events import IncomingMessage, OutgoingResponse, Platform, PlatformUser, ResponseKind


def _payload(command: str, text: str = "", **overrides) -> SlashCommandPayload:
    base = {
        "command": command,
        "text": text,
        "user_id": "U0123",
        "user_name": "zonda",
        "channel_id": "C9999",
        "response_url": "https://hooks.slack.com/commands/T0/1/abc",
        "team_id": "T0",
    }
    base.update(overrides)
    return SlashCommandPayload(**base)


def _adapter(slack_config: SlackConfig | None = None) -> SlackAdapter:
    return SlackAdapter(SlackClient(), slack_config or SlackConfig())


# --- parse() ----------------------------------------------------------


def test_parse_command_with_no_args_keeps_just_slash_name():
    msg = _adapter().parse(_payload("/help"))
    assert msg is not None
    assert msg.text == "/help"
    assert msg.user.norm_id == "slack:U0123"
    assert msg.chat_id == "C9999"


def test_parse_command_with_args_rebuilds_canonical_text():
    """Slack splits command/text; we recombine to the form the dispatcher
    + router rewrite expects (same as Telegram /service_restart text)."""
    msg = _adapter().parse(_payload("/service", "restart hello --host gce"))
    assert msg is not None
    assert msg.text == "/service restart hello --host gce"


def test_parse_keeps_response_url_in_raw_for_send():
    """send() needs the response_url back to deliver the deferred reply."""
    payload = _payload("/help", response_url="https://hooks.slack.com/commands/T0/123/xyz")
    msg = _adapter().parse(payload)
    assert msg is not None
    assert msg.raw["response_url"] == "https://hooks.slack.com/commands/T0/123/xyz"


def test_parse_tolerates_missing_user_name():
    """Slack stops sending user_name in some DM contexts; don't crash."""
    msg = _adapter().parse(_payload("/help", user_name=None))
    assert msg is not None
    assert msg.user.handle is None


# --- send() visibility resolver --------------------------------------


def _resp(text: str = "ok", risk: str | None = None, command_name: str | None = None) -> OutgoingResponse:
    return OutgoingResponse(kind=ResponseKind.TEXT, text=text, risk=risk, command_name=command_name)


def test_visibility_global_in_channel_always_broadcasts():
    cfg = SlackConfig(reply_visibility="in_channel")
    adapter = _adapter(cfg)
    # Even a PRIVILEGED command goes in_channel under this mode.
    assert adapter._resolve_in_channel(_resp(risk="privileged", command_name="ssh")) is True
    assert adapter._resolve_in_channel(_resp(risk="safe", command_name="help")) is True


def test_visibility_global_ephemeral_always_private():
    cfg = SlackConfig(reply_visibility="ephemeral")
    adapter = _adapter(cfg)
    assert adapter._resolve_in_channel(_resp(risk="safe", command_name="help")) is False
    assert adapter._resolve_in_channel(_resp(risk="privileged", command_name="ssh")) is False


def test_visibility_by_risk_safe_broadcasts_privileged_private():
    """The headline mode the user asked for: collaborative ops can see
    each other's diagnostic SAFE invocations, but SSH output stays
    private to the operator."""
    cfg = SlackConfig(reply_visibility="by_risk")
    adapter = _adapter(cfg)
    assert adapter._resolve_in_channel(_resp(risk="safe", command_name="help")) is True
    assert adapter._resolve_in_channel(_resp(risk="safe", command_name="health")) is True
    assert adapter._resolve_in_channel(_resp(risk="privileged", command_name="ssh")) is False
    assert adapter._resolve_in_channel(_resp(risk="privileged", command_name="service_restart")) is False


def test_visibility_per_command_override_wins_over_global():
    """The user's example case: /whoami is SAFE but reveals user IDs;
    they want it ephemeral even though by_risk would broadcast."""
    cfg = SlackConfig(
        reply_visibility="by_risk",
        visibility_overrides={"whoami": "ephemeral", "service_restart": "in_channel"},
    )
    adapter = _adapter(cfg)
    # Override flips SAFE→ephemeral
    assert adapter._resolve_in_channel(_resp(risk="safe", command_name="whoami")) is False
    # Override flips PRIVILEGED→in_channel
    assert adapter._resolve_in_channel(_resp(risk="privileged", command_name="service_restart")) is True
    # Non-overridden command still follows by_risk
    assert adapter._resolve_in_channel(_resp(risk="safe", command_name="help")) is True


def test_visibility_unknown_risk_falls_back_to_ephemeral():
    """ACL deny / unknown command never set risk; default to private
    so an error message isn't broadcast."""
    cfg = SlackConfig(reply_visibility="by_risk")
    adapter = _adapter(cfg)
    assert adapter._resolve_in_channel(_resp(risk=None, command_name=None)) is False


# --- command echo + redaction ---------------------------------------


def test_command_echo_prepends_typed_text_to_reply():
    """Slack hides slash commands from channel history; echo prevents
    bot-soliloquy reads of past replies."""
    body = SlackAdapter._with_command_echo("/help", "Available commands: ...")
    assert body.startswith("> `/help`")
    assert "Available commands" in body
    # Blank line between echo and reply for readability
    assert "`/help`\n\n" in body


def test_command_echo_redacts_otp_code():
    """OTP codes must never land in chat history — Slack's ephemeral
    messages persist, and a future in_channel visibility override
    would broadcast the code to the channel."""
    body = SlackAdapter._with_command_echo("/otp 123456", "ok, restarted api")
    assert "123456" not in body
    assert "/otp <redacted>" in body
    assert "ok, restarted api" in body


def test_command_echo_with_empty_reply_still_shows_command():
    """A handler that returned empty text still benefits from echo,
    so the user can see their own command went somewhere."""
    body = SlackAdapter._with_command_echo("/whoami", "")
    assert body == "> `/whoami`"


def test_command_echo_with_router_subcommand_args():
    """Multi-word commands echo verbatim — preserves --host flags etc."""
    body = SlackAdapter._with_command_echo("/service restart hello --host gce", "$ hello restart on gce\nexit=0")
    assert "> `/service restart hello --host gce`" in body
    assert "exit=0" in body


# --- send() honors displayed_command override -----------------------


@pytest.mark.asyncio
async def test_send_uses_displayed_command_when_set(monkeypatch):
    """Regression for the OTP-resume case: user types `/otp 123456`
    but what actually ran is the resumed `/ssh hello uptime`. The
    echo should show the resumed command (so the chat reads as a
    transcript) — NOT `/otp <redacted>` above an "unknown host: hello"
    error that mentions a host the user never typed in /otp."""
    from datetime import datetime, timezone

    captured: dict = {}

    async def _fake_send(url, text, *, in_channel, replace_original=False):
        captured["text"] = text

    adapter = _adapter()
    monkeypatch.setattr(adapter._client, "send_response", _fake_send)

    msg = IncomingMessage(
        platform=Platform.SLACK,
        user=PlatformUser(platform=Platform.SLACK, raw_id="U0", handle=None),
        chat_id="C0",
        text="/otp 123456",  # what the user actually typed
        message_id=None,
        raw={"response_url": "https://hooks.slack.com/commands/T0/1/abc"},
        received_at=datetime.now(timezone.utc),
    )
    resp = OutgoingResponse(
        kind=ResponseKind.TEXT,
        text="unknown host: hello (known: gce)",
        risk="privileged",
        command_name="ssh",
        displayed_command="/ssh hello uptime",  # what /otp actually resumed
    )
    await adapter.send(msg, resp)
    body = captured["text"]
    # Echo shows the resumed command, not /otp.
    assert "> `/ssh hello uptime`" in body
    # And no `/otp` echo / no leaked OTP code.
    assert "/otp" not in body
    assert "123456" not in body
    # Reply text preserved beneath the echo.
    assert "unknown host: hello" in body


@pytest.mark.asyncio
async def test_send_falls_back_to_msg_text_without_displayed_command(monkeypatch):
    """Normal (non-OTP-resume) flow: dispatcher doesn't set displayed_command,
    adapter echoes literally what the user typed."""
    from datetime import datetime, timezone

    captured: dict = {}

    async def _fake_send(url, text, *, in_channel, replace_original=False):
        captured["text"] = text

    adapter = _adapter()
    monkeypatch.setattr(adapter._client, "send_response", _fake_send)

    msg = IncomingMessage(
        platform=Platform.SLACK,
        user=PlatformUser(platform=Platform.SLACK, raw_id="U0", handle=None),
        chat_id="C0",
        text="/health",
        message_id=None,
        raw={"response_url": "https://hooks.slack.com/commands/T0/1/abc"},
        received_at=datetime.now(timezone.utc),
    )
    resp = OutgoingResponse(
        kind=ResponseKind.TEXT,
        text="api: OK",
        risk="safe",
        command_name="health",
        # displayed_command not set — adapter should fall back
    )
    await adapter.send(msg, resp)
    assert "> `/health`" in captured["text"]
    assert "api: OK" in captured["text"]


# --- send() integration --------------------------------------------


@pytest.mark.asyncio
async def test_send_no_response_url_in_raw_drops_silently():
    """Without a response_url we have nowhere to deliver — must not
    raise (would crash the BackgroundTask without anyone seeing it)."""
    from datetime import datetime, timezone

    msg = IncomingMessage(
        platform=Platform.SLACK,
        user=PlatformUser(platform=Platform.SLACK, raw_id="U0", handle=None),
        chat_id="C0",
        text="/help",
        message_id=None,
        raw={},  # no response_url
        received_at=datetime.now(timezone.utc),
    )
    await _adapter().send(msg, _resp())
