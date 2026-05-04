"""Tests for SlackSocketDaemon (Phase 6b).

Strategy mirrors test_daemon.py for Telegram polling: the daemon
takes injectable `fetch_url` + `connect_ws` callables defaulting to
the real-network paths. Tests pass in-memory fakes that yield queued
events and capture sent ACKs, so the entire WebSocket protocol is
exercised without network I/O.

This isolates the daemon from `websockets` library quirks and lets
us assert on:
  - hello → connected
  - slash_commands → ACK first, dispatch second
  - disconnect → return cleanly so the outer loop reconnects
  - bad payload → drop without dying
  - unknown event types → ignore quietly
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from bot_cmder.adapters.slack.adapter import SlackAdapter
from bot_cmder.adapters.slack.client import SlackClient
from bot_cmder.adapters.slack.socket import SlackSocketDaemon
from bot_cmder.config.schema import SlackConfig


def _slash_event(envelope_id: str, command: str = "/help", text: str = "") -> dict[str, Any]:
    """Slack-shaped slash_commands envelope. Payload mirrors the
    form-decoded HTTP webhook body so the SAME SlashCommandPayload
    pydantic model parses both ingestion paths."""
    return {
        "envelope_id": envelope_id,
        "type": "slash_commands",
        "payload": {
            "command": command,
            "text": text,
            "user_id": "U0",
            "user_name": "zonda",
            "channel_id": "C0",
            "response_url": "https://hooks.slack.com/commands/T0/1/abc",
            "team_id": "T0",
        },
    }


class _FakeWebSocket:
    """In-memory stand-in for a websockets connection.

    Implements the async iterator + async context manager protocol the
    daemon's _stream uses, plus a `send()` that captures ACKs.
    """

    def __init__(self, frames: list[Any]) -> None:
        # Each frame: a dict to be JSON-encoded (delivered as text), a
        # raw str to deliver verbatim, or an Exception to raise.
        self._frames = list(frames)
        self.sent: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeWebSocket:
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    def __aiter__(self) -> _FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        if not self._frames:
            # Real WSS waits for next frame indefinitely. If we raised
            # StopAsyncIteration here the daemon's reconnect loop would
            # spin tight (none of fetch_url/connect_ws yield to the
            # loop in the fakes), and `task.cancel()` would never get
            # a checkpoint to land on — test hangs forever. Block on a
            # real `asyncio.sleep` so cancellation has somewhere to
            # propagate, and so the iteration mirrors production
            # semantics ("idle WS" = waiting, not "EOF").
            await asyncio.sleep(3600)
            raise StopAsyncIteration  # unreachable but pleases type checkers
        frame = self._frames.pop(0)
        if isinstance(frame, Exception):
            raise frame
        if isinstance(frame, dict):
            return json.dumps(frame)
        return frame  # str

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


def _build_daemon(
    *,
    frames: list[Any] | None = None,
    fetch_url_result: str = "wss://fake.example/socket",
    fetch_url_error: Exception | None = None,
    dispatch_result=None,
) -> tuple[SlackSocketDaemon, _FakeWebSocket, AsyncMock]:
    """Wire up a daemon with fake WS + stubbed dispatcher."""
    fake_ws = _FakeWebSocket(frames=frames or [])

    async def _fetch():
        if fetch_url_error is not None:
            raise fetch_url_error
        return fetch_url_result

    def _connect(_url: str):
        return fake_ws

    client = SlackClient()
    adapter = SlackAdapter(client, SlackConfig())
    dispatcher = AsyncMock()
    dispatcher.dispatch = AsyncMock(return_value=dispatch_result)

    daemon = SlackSocketDaemon(
        app_token="xapp-fake",
        adapter=adapter,
        dispatcher=dispatcher,
        fetch_url=_fetch,
        connect_ws=_connect,
    )
    return daemon, fake_ws, dispatcher


# --- happy path -------------------------------------------------------


@pytest.mark.asyncio
async def test_hello_event_logged_no_ack_no_dispatch():
    """`hello` is informational only — no envelope_id, no payload."""
    daemon, ws, dispatcher = _build_daemon(frames=[{"type": "hello", "num_connections": 1}])
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # No ACK sent (nothing to acknowledge), no dispatch attempted.
    assert ws.sent == []
    assert dispatcher.dispatch.await_count == 0


@pytest.mark.asyncio
async def test_slash_command_acks_first_then_dispatches():
    """Critical contract: ACK envelope_id WITHIN 3s; dispatch happens
    in a background task so a slow handler can't make us miss the
    deadline."""
    daemon, ws, dispatcher = _build_daemon(frames=[_slash_event("env-1")])
    task = asyncio.create_task(daemon.run())
    # Need a beat for the dispatch task to schedule.
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # ACK frame is exactly the envelope_id back, no other fields.
    assert ws.sent == [{"envelope_id": "env-1"}]
    # Dispatch ran for the parsed slash command.
    assert dispatcher.dispatch.await_count == 1
    msg = dispatcher.dispatch.await_args.args[0]
    assert msg.text == "/help"
    assert msg.user.norm_id == "slack:U0"


@pytest.mark.asyncio
async def test_disconnect_event_returns_so_outer_loop_reconnects():
    """Slack sends `disconnect` for graceful capacity rebalancing.
    `_stream` returns; outer `run` loop then fetches a fresh WSS URL
    and reconnects without backoff."""
    # First connection: hello → disconnect (stream returns)
    # Second connection: empty stream (we'll cancel mid-await)
    fake_ws_1 = _FakeWebSocket([{"type": "hello"}, {"type": "disconnect", "reason": "warning"}])
    fake_ws_2 = _FakeWebSocket([])  # next reconnect's stream
    connect_calls = []

    def _connect(url):
        connect_calls.append(url)
        return fake_ws_1 if len(connect_calls) == 1 else fake_ws_2

    fetch_calls = []

    async def _fetch():
        fetch_calls.append(None)
        return f"wss://fake.example/socket-{len(fetch_calls)}"

    client = SlackClient()
    adapter = SlackAdapter(client, SlackConfig())
    dispatcher = AsyncMock()
    daemon = SlackSocketDaemon(
        app_token="xapp-fake",
        adapter=adapter,
        dispatcher=dispatcher,
        fetch_url=_fetch,
        connect_ws=_connect,
    )

    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.1)  # let it cycle through disconnect → reconnect
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Reconnect happened: two fetch calls and two connect calls.
    assert len(fetch_calls) >= 2
    assert len(connect_calls) >= 2


# --- error / edge cases ----------------------------------------------


@pytest.mark.asyncio
async def test_401_from_apps_open_stops_daemon_no_retry():
    """Bad app token → 401. No point retrying; surface loudly + exit."""
    request = httpx.Request("POST", "https://slack.com/api/apps.connections.open")
    response = httpx.Response(401, request=request)
    err = httpx.HTTPStatusError("unauthorized", request=request, response=response)
    daemon, _ws, _disp = _build_daemon(fetch_url_error=err)

    # If 401 isn't fatal, this test would hang forever — wait_for catches it.
    await asyncio.wait_for(daemon.run(), timeout=2.0)


@pytest.mark.asyncio
async def test_transient_5xx_from_apps_open_backs_off():
    """A 502 is retryable — backoff, retry, eventually succeed."""
    call_count = 0

    async def _flaky_fetch():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            request = httpx.Request("POST", "https://slack.com/api/apps.connections.open")
            response = httpx.Response(502, request=request)
            raise httpx.HTTPStatusError("bad gateway", request=request, response=response)
        return "wss://fake.example/socket"

    fake_ws = _FakeWebSocket([])  # connect once, then idle until cancel
    client = SlackClient()
    adapter = SlackAdapter(client, SlackConfig())
    dispatcher = AsyncMock()
    daemon = SlackSocketDaemon(
        app_token="xapp-fake",
        adapter=adapter,
        dispatcher=dispatcher,
        fetch_url=_flaky_fetch,
        connect_ws=lambda _u: fake_ws,
    )

    task = asyncio.create_task(daemon.run())
    # First fetch fails, daemon backs off ~1s, second fetch succeeds.
    await asyncio.sleep(1.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert call_count >= 2  # at least one retry happened


@pytest.mark.asyncio
async def test_unknown_event_type_ignored():
    """Slack may add new event shapes; unknown ones must not crash
    the loop. ACK still happens (we received it); dispatch is skipped."""
    weird = {"envelope_id": "env-x", "type": "future_event_we_dont_know", "payload": {}}
    daemon, ws, dispatcher = _build_daemon(frames=[weird])
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Still ACKed (fulfills the protocol contract)
    assert ws.sent == [{"envelope_id": "env-x"}]
    # But no dispatch
    assert dispatcher.dispatch.await_count == 0


@pytest.mark.asyncio
async def test_malformed_slash_payload_logs_and_drops_keeps_loop_alive():
    """A garbage payload (missing required fields) must not crash the
    daemon — we ACK and move on so the next event still gets through."""
    bad = {"envelope_id": "bad-1", "type": "slash_commands", "payload": {"command": "/help"}}  # missing user_id etc
    good = _slash_event("good-1")
    daemon, ws, dispatcher = _build_daemon(frames=[bad, good])
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Both events ACKed (protocol-level), but only the good one
    # reached the dispatcher.
    sent_envelope_ids = [s["envelope_id"] for s in ws.sent]
    assert "bad-1" in sent_envelope_ids
    assert "good-1" in sent_envelope_ids
    assert dispatcher.dispatch.await_count == 1


@pytest.mark.asyncio
async def test_non_json_frame_does_not_crash_loop():
    """If Slack ever sent garbage we couldn't decode (server bug), we
    log and skip rather than die on json.JSONDecodeError."""
    daemon, ws, dispatcher = _build_daemon(frames=["not-valid-json", _slash_event("after")])
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The good event after the bad frame still got through.
    assert dispatcher.dispatch.await_count == 1
