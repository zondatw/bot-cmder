"""Tests for TelegramDaemon (Phase 6a polling mode).

Strategy: stub TelegramClient with an in-memory fake whose
get_updates returns whatever the test queues up. This keeps tests
hermetic (no respx) AND lets us assert on offset advancement /
backoff timing / cancellation semantics that are hard to drive
through HTTP-level mocks.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from bot_cmder.adapters.telegram.adapter import TelegramAdapter
from bot_cmder.adapters.telegram.daemon import TelegramDaemon


def _msg_update(update_id: int, text: str = "/help") -> dict[str, Any]:
    """Build a minimal Telegram Update dict the adapter can parse."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1700000000,
            "chat": {"id": 100, "type": "private"},
            "from": {"id": 200, "is_bot": False, "first_name": "Zonda"},
            "text": text,
        },
    }


class _FakeClient:
    """Stand-in for TelegramClient that returns queued update batches."""

    def __init__(self, batches: list[Any] | None = None, webhook_url: str = "") -> None:
        # Each entry: list[dict] of updates, OR an Exception to raise.
        self._batches = list(batches or [])
        self._webhook_url = webhook_url
        self.delete_webhook_calls: list[dict[str, Any]] = []
        self.get_updates_calls: list[dict[str, Any]] = []

    async def get_webhook_info(self) -> dict[str, Any]:
        return {"url": self._webhook_url}

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> dict[str, Any]:
        self.delete_webhook_calls.append({"drop_pending_updates": drop_pending_updates})
        return {"ok": True}

    async def get_updates(
        self, *, offset: int | None = None, timeout_s: int = 25, allowed_updates: list[str] | None = None
    ) -> list[dict[str, Any]]:
        self.get_updates_calls.append({"offset": offset, "timeout_s": timeout_s})
        if not self._batches:
            # Idle forever once queue drains — simulates a quiet
            # Telegram. Tests that need the loop to exit must cancel
            # the task themselves.
            await asyncio.sleep(3600)
            return []
        next_batch = self._batches.pop(0)
        if isinstance(next_batch, Exception):
            raise next_batch
        return next_batch


def _build_daemon(client: _FakeClient, dispatch_result=None) -> tuple[TelegramDaemon, AsyncMock]:
    """Build a daemon with a real adapter wrapping the fake client + a
    stubbed dispatcher whose dispatch() we can assert on."""
    # The adapter needs a real TelegramClient signature for typing,
    # but since we're not exercising send() here, the fake works.
    adapter = TelegramAdapter(client)  # type: ignore[arg-type]
    dispatcher = AsyncMock()
    dispatcher.dispatch = AsyncMock(return_value=dispatch_result)
    daemon = TelegramDaemon(client, adapter, dispatcher, long_poll_timeout_s=1)  # type: ignore[arg-type]
    return daemon, dispatcher


# --- offset bookkeeping -----------------------------------------------


@pytest.mark.asyncio
async def test_first_poll_passes_no_offset():
    """First call has offset=None — Telegram returns whatever's queued."""
    client = _FakeClient(batches=[[_msg_update(10)]])
    daemon, _dispatcher = _build_daemon(client)
    task = asyncio.create_task(daemon.run())
    # Let the daemon do one poll then sleep on the empty queue.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.get_updates_calls[0]["offset"] is None


@pytest.mark.asyncio
async def test_offset_advances_past_seen_updates():
    """After processing update 10, next poll passes offset=11."""
    client = _FakeClient(batches=[[_msg_update(10)], [_msg_update(11), _msg_update(12)]])
    daemon, _dispatcher = _build_daemon(client)
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # First poll: no offset (initial state)
    # Second poll: offset = 11 (10 + 1)
    # Third poll: offset = 13 (12 + 1)
    offsets = [c["offset"] for c in client.get_updates_calls]
    assert offsets[0] is None
    assert offsets[1] == 11
    assert offsets[2] == 13


@pytest.mark.asyncio
async def test_malformed_update_does_not_block_loop():
    """A garbage update advances offset past it and the daemon keeps
    going — without this, one bad payload would deadlock polling."""
    bad = {"update_id": 50, "message": "this should be a dict, not a string"}
    good = _msg_update(51)
    client = _FakeClient(batches=[[bad, good]])
    daemon, dispatcher = _build_daemon(client)
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Bad update was skipped (no dispatch); good update went through.
    assert dispatcher.dispatch.await_count == 1
    # Offset advanced past BOTH so neither gets re-polled forever.
    # Second poll's offset should be 52 (max(50, 51) + 1).
    assert client.get_updates_calls[1]["offset"] == 52


# --- dispatch flow ----------------------------------------------------


@pytest.mark.asyncio
async def test_each_message_is_dispatched_once():
    client = _FakeClient(batches=[[_msg_update(1), _msg_update(2), _msg_update(3)]])
    daemon, dispatcher = _build_daemon(client, dispatch_result=None)
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert dispatcher.dispatch.await_count == 3


# --- error handling / backoff -----------------------------------------


@pytest.mark.asyncio
async def test_409_conflict_stops_daemon_gracefully():
    """A webhook re-registered out from under us → 409. Daemon should
    log and stop, NOT keep silently re-deleting webhook every loop."""
    request = httpx.Request("POST", "https://api.telegram.org/botfake/getUpdates")
    response = httpx.Response(409, request=request)
    err = httpx.HTTPStatusError("conflict", request=request, response=response)
    client = _FakeClient(batches=[err])
    daemon, _dispatcher = _build_daemon(client)
    # No task.cancel() — the daemon should exit on its own when it
    # sees 409. asyncio.wait_for with a timeout catches the case
    # where it incorrectly loops forever.
    await asyncio.wait_for(daemon.run(), timeout=2.0)


@pytest.mark.asyncio
async def test_transient_error_backs_off_and_retries():
    """Network blip → backoff → next poll. Doesn't crash the daemon."""
    # First call raises, second call returns one update, daemon
    # processes it. Tests that backoff sleep happens AND that we
    # recover after the error.
    network_err = httpx.ConnectError("connection refused")
    client = _FakeClient(batches=[network_err, [_msg_update(99)]])
    daemon, dispatcher = _build_daemon(client)
    task = asyncio.create_task(daemon.run())
    # Backoff is 1s on first failure. Wait long enough for retry.
    await asyncio.sleep(1.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Update from the second batch was dispatched.
    assert dispatcher.dispatch.await_count == 1


# --- webhook mutex ----------------------------------------------------


@pytest.mark.asyncio
async def test_existing_webhook_is_deleted_at_startup():
    """Mode flip from webhook → polling auto-clears the registered URL
    so the operator doesn't have to remember the manual deleteWebhook."""
    client = _FakeClient(webhook_url="https://example.com/hook")
    daemon, _dispatcher = _build_daemon(client)
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(client.delete_webhook_calls) == 1


@pytest.mark.asyncio
async def test_drop_pending_flag_propagates_to_delete_webhook():
    """When operator sets TELEGRAM_POLLING_DROP_PENDING=true, the
    daemon passes drop_pending_updates=true so queued backlog clears."""
    client = _FakeClient(webhook_url="")
    adapter = TelegramAdapter(client)  # type: ignore[arg-type]
    dispatcher = AsyncMock()
    daemon = TelegramDaemon(
        client,  # type: ignore[arg-type]
        adapter,
        dispatcher,
        long_poll_timeout_s=1,
        drop_pending_on_start=True,
    )
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.delete_webhook_calls[0]["drop_pending_updates"] is True
