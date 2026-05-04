"""Tests for DiscordGatewayDaemon (Phase 6c).

Strategy: in-memory `_FakeWS` mirrors the Slack socket test pattern,
but the Discord protocol is more involved — IDENTIFY, HEARTBEAT(_ACK),
RESUME, INVALID_SESSION, RECONNECT, sequence tracking. Each test
queues a specific frame sequence and asserts the daemon's reaction
(messages it sends back, internal state changes via the public API).

State to verify:
  - HELLO arrival → IDENTIFY sent (fresh) OR RESUME sent (have session)
  - DISPATCH READY → session_id + resume_url + bot_user_id captured
  - DISPATCH MESSAGE_CREATE → adapter.parse + dispatcher.dispatch
  - HEARTBEAT_ACK → unacked counter resets
  - 2 unacked heartbeats → connection closed (zombie detection)
  - INVALID_SESSION (resumable=False) → state wiped, daemon eventually
    re-IDENTIFYs
  - RECONNECT op → exits stream so outer loop reconnects (RESUME)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from bot_cmder.adapters.discord.adapter import DiscordAdapter
from bot_cmder.adapters.discord.client import DiscordClient
from bot_cmder.adapters.discord.gateway import DiscordGatewayDaemon

# Discord opcodes (mirrored from gateway.py for clarity in tests)
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11


def _hello(interval_ms: int = 41250) -> dict[str, Any]:
    """A canonical HELLO frame with the heartbeat interval Discord
    sends in production (~41s). Tests usually override to a much
    smaller interval so heartbeat-related cases run fast."""
    return {"op": OP_HELLO, "d": {"heartbeat_interval": interval_ms}}


def _ready(session_id: str = "sess-abc", bot_id: str = "9999") -> dict[str, Any]:
    return {
        "op": OP_DISPATCH,
        "s": 1,
        "t": "READY",
        "d": {
            "session_id": session_id,
            "resume_gateway_url": "wss://resume.example/?v=10",
            "user": {"id": bot_id, "username": "sre_bot"},
        },
    }


def _message_create(
    user_id: str = "1111",
    content: str = "<@9999> help",
    channel_id: str = "ch-1",
    is_dm: bool = False,
) -> dict[str, Any]:
    return {
        "op": OP_DISPATCH,
        "s": 2,
        "t": "MESSAGE_CREATE",
        "d": {
            "id": "msg-1",
            "content": content,
            "channel_id": channel_id,
            "guild_id": None if is_dm else "guild-1",
            "author": {"id": user_id, "username": "alice", "bot": False},
            "mentions": [{"id": "9999"}] if "<@9999>" in content else [],
        },
    }


class _FakeWS:
    """In-memory Discord Gateway WebSocket stand-in.

    Same async iterator + context manager protocol the daemon's
    `_stream` uses. Send-side captures frames so tests can assert on
    IDENTIFY / RESUME / HEARTBEAT payloads. Closing via `close()`
    raises ConnectionClosed on the next read, matching real `websockets`
    behavior.
    """

    def __init__(self, frames: list[Any]) -> None:
        self._frames = list(frames)
        self.sent: list[dict[str, Any]] = []
        self._closed_code: int | None = None

    async def __aenter__(self) -> _FakeWS:
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    def __aiter__(self) -> _FakeWS:
        return self

    async def __anext__(self) -> str:
        if self._closed_code is not None:
            from websockets.exceptions import ConnectionClosed
            from websockets.frames import Close

            raise ConnectionClosed(rcvd=Close(self._closed_code, "test close"), sent=None)
        if not self._frames:
            # Like the Slack fake — sleep so cancellation has a real
            # checkpoint to land on.
            await asyncio.sleep(3600)
            raise StopAsyncIteration  # unreachable
        frame = self._frames.pop(0)
        if isinstance(frame, Exception):
            raise frame
        if isinstance(frame, dict):
            return json.dumps(frame)
        return frame  # str

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self._closed_code = code


def _build(frames: list[Any], dispatch_result=None) -> tuple[DiscordGatewayDaemon, _FakeWS, AsyncMock]:
    fake_ws = _FakeWS(frames)
    client = DiscordClient(bot_token="fake-bot-token", application_id="111")
    adapter = DiscordAdapter(client)
    dispatcher = AsyncMock()
    dispatcher.dispatch = AsyncMock(return_value=dispatch_result)
    daemon = DiscordGatewayDaemon(
        bot_token="fake-bot-token",
        adapter=adapter,
        dispatcher=dispatcher,
        connect_ws=lambda _url: fake_ws,
    )
    return daemon, fake_ws, dispatcher


# --- IDENTIFY / READY flow -----------------------------------------------


@pytest.mark.asyncio
async def test_hello_triggers_identify_with_intents():
    """First HELLO on a fresh session causes IDENTIFY (op 2) with the
    bot token + intents bitmask. RESUME is NOT sent because session_id
    is None at this point."""
    daemon, ws, _disp = _build([_hello()])
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    identify = next(s for s in ws.sent if s.get("op") == OP_IDENTIFY)
    assert identify["d"]["token"] == "fake-bot-token"
    # Intents bitmask = GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT
    expected = (1 << 9) | (1 << 12) | (1 << 15)
    assert identify["d"]["intents"] == expected
    # No RESUME in the same handshake
    assert not any(s.get("op") == OP_RESUME for s in ws.sent)


@pytest.mark.asyncio
async def test_ready_captures_session_state_and_bot_id():
    """READY (DISPATCH event) carries the session_id, resume URL, and
    our own bot user ID. The latter is plumbed into the adapter so
    mention-stripping works on subsequent MESSAGE_CREATEs."""
    daemon, ws, _disp = _build([_hello(), _ready(session_id="my-sess", bot_id="42")])
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert daemon._session_id == "my-sess"
    assert daemon._resume_url == "wss://resume.example/?v=10"
    # Adapter learned its own bot ID from the READY payload
    assert daemon._adapter._bot_user_id == "42"


# --- MESSAGE_CREATE dispatch --------------------------------------------


@pytest.mark.asyncio
async def test_mention_message_dispatched_to_dispatcher():
    """A `<@bot> help` mention in a guild channel reaches the
    dispatcher with the canonical `/help` text shape."""
    daemon, _ws, dispatcher = _build([_hello(), _ready(), _message_create()])
    task = asyncio.create_task(daemon.run())
    # Dispatch happens in a separate task; need a beat for it to schedule.
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert dispatcher.dispatch.await_count == 1
    msg = dispatcher.dispatch.await_args.args[0]
    assert msg.text == "/help"
    assert msg.user.norm_id == "discord:1111"


@pytest.mark.asyncio
async def test_dm_without_mention_still_dispatched():
    """In DMs every message addressed to the bot is implicitly a
    command — no @-mention needed."""
    daemon, _ws, dispatcher = _build(
        [_hello(), _ready(), _message_create(content="health", is_dm=True)],
    )
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert dispatcher.dispatch.await_count == 1
    assert dispatcher.dispatch.await_args.args[0].text == "/health"


@pytest.mark.asyncio
async def test_unrelated_guild_message_ignored():
    """A guild message that doesn't @-mention our bot is NOT a command;
    the dispatcher is never called."""
    daemon, _ws, dispatcher = _build(
        [_hello(), _ready(), _message_create(content="just chatting", is_dm=False)],
    )
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert dispatcher.dispatch.await_count == 0


@pytest.mark.asyncio
async def test_bot_message_skipped_no_loop_risk():
    """Messages from any bot account (including ourselves) must
    NEVER be dispatched. Otherwise our own replies could trigger a
    runaway loop."""
    bot_msg = _message_create(content="<@9999> help")
    bot_msg["d"]["author"]["bot"] = True
    daemon, _ws, dispatcher = _build([_hello(), _ready(), bot_msg])
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert dispatcher.dispatch.await_count == 0


# --- RESUME path --------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_sent_when_session_exists_after_reconnect():
    """If we've already received a READY (have session_id) and
    reconnect, the next HELLO triggers RESUME (op 6) instead of
    IDENTIFY (op 2). Sequence number is included so Discord can
    replay missed events."""
    # Simulate a daemon that already went through one successful
    # IDENTIFY + READY, then reconnects — by manually setting state.
    daemon, ws, _disp = _build([_hello()])
    daemon._session_id = "carried-over"
    daemon._resume_url = "wss://test"
    daemon._last_seq = 42

    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    resume = next(s for s in ws.sent if s.get("op") == OP_RESUME)
    assert resume["d"]["session_id"] == "carried-over"
    assert resume["d"]["seq"] == 42
    # And NO fresh IDENTIFY in the same handshake
    assert not any(s.get("op") == OP_IDENTIFY for s in ws.sent)


@pytest.mark.asyncio
async def test_invalid_session_resets_state_for_fresh_identify():
    """INVALID_SESSION (op 9) with payload=False means the session
    is dead. Daemon must wipe state so the next reconnect does a
    fresh IDENTIFY rather than a doomed RESUME."""
    daemon, _ws, _disp = _build(
        [
            _hello(),
            _ready(session_id="dead-sess"),
            {"op": OP_INVALID_SESSION, "d": False},
        ]
    )
    task = asyncio.create_task(daemon.run())
    # daemon needs time to: receive READY (state set) → receive
    # INVALID_SESSION (state wiped) → sleep 2s before retry
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # State was wiped after the invalid session
    assert daemon._session_id is None
    assert daemon._last_seq is None


@pytest.mark.asyncio
async def test_reconnect_op_exits_stream_for_resume():
    """RECONNECT (op 7) is the server saying "please reconnect" —
    we exit `_stream` so the outer loop reconnects (RESUME path
    since session_id is preserved)."""
    daemon, _ws, _disp = _build(
        [
            _hello(),
            _ready(session_id="keep-me"),
            {"op": OP_RECONNECT},
        ]
    )
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Session preserved across the requested reconnect
    assert daemon._session_id == "keep-me"


# --- heartbeat ----------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_ack_resets_unacked_counter():
    """Receiving HEARTBEAT_ACK clears the unacked counter; daemon
    won't trip the zombie detector."""
    daemon, _ws, _disp = _build(
        [
            _hello(interval_ms=10000),  # 10s — too long for our test wait
            _ready(),
            {"op": OP_HEARTBEAT_ACK},
        ]
    )
    # Manually pump the unacked counter up first to verify reset.
    daemon._heartbeat_unacked = 1
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert daemon._heartbeat_unacked == 0


# --- malformed payloads -------------------------------------------------


@pytest.mark.asyncio
async def test_non_json_frame_does_not_kill_loop():
    daemon, _ws, dispatcher = _build([_hello(), _ready(), "not-json", _message_create()])
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The good MESSAGE_CREATE after the bad frame still dispatched
    assert dispatcher.dispatch.await_count == 1


@pytest.mark.asyncio
async def test_bad_message_create_payload_logged_not_fatal():
    """A MESSAGE_CREATE event with a malformed `d` (missing required
    fields) is logged + skipped; the loop keeps going."""
    bad = {"op": OP_DISPATCH, "s": 2, "t": "MESSAGE_CREATE", "d": {"id": "x"}}  # missing channel_id, author, etc
    daemon, _ws, dispatcher = _build([_hello(), _ready(), bad, _message_create()])
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Bad event dropped (no dispatch); good one still went through.
    assert dispatcher.dispatch.await_count == 1
