"""DiscordGatewayDaemon — WebSocket-based ingestion for Discord (Phase 6c).

Why this exists
---------------
Phase 4's HTTP Interactions endpoint requires a public HTTPS URL
Discord can POST to. For home labs / NAT / restrictive corp egress
that's the friction point. The Gateway flips connection direction:
the bot dials OUT to Discord's WSS endpoint and chat events arrive
over the open connection. No inbound port, no domain, no tunnel.

THE BIG TRADE-OFF
-----------------
**Discord Gateway does NOT deliver slash command interactions.**
Slash commands are a separate Discord product (Application Commands)
and ALWAYS go through HTTP Interactions, even when Gateway is
connected. So Gateway mode loses the slash-command UX:

  - No autocomplete menu
  - No typed args (`code` STRING option for /otp etc.)
  - No automatic redaction in chat
  - No ephemeral replies (everything is a regular channel message)

Gateway mode's UX is plain text:
  - DM the bot:        `service restart hello --host gce`
  - In a guild:        `@sre_bot service restart hello --host gce`

The adapter normalizes both into the canonical `/cmd args` text the
dispatcher expects, so downstream behavior (ACL, OTP gate, audit) is
identical to Interactions mode. The /otp gate still functions; the
user just submits the code as `@sre_bot otp 123456` instead of
`/otp code:123456`.

D3 dual-mode posture
--------------------
Operators should pick ONE mode per deployment instance:
  - `DISCORD_MODE=interactions` (default) — webhook + slash commands
  - `DISCORD_MODE=gateway` — WSS + @-mention / DM commands

Both modes can coexist across instances (e.g. prod uses interactions,
dev lab uses gateway), but a single instance only enables one path.
The other path is silently inactive — Discord still tries to deliver
slash commands to the registered Interactions URL even when this
daemon is running, but our `/webhooks/discord` route is unmounted in
gateway mode so Discord just gets 404s for them. That's fine: the
operator chose gateway knowing slash commands won't work, and the
user sees Discord's "didn't respond" UI and falls back to @-mention.

Privileged intents
------------------
The Gateway only delivers `MESSAGE_CONTENT` when the operator has
ENABLED that privileged intent in the Discord dev portal:
  Bot page → Privileged Gateway Intents → Message Content Intent
Without it, MESSAGE_CREATE events arrive with `content` empty and the
adapter has nothing to dispatch on. Documented as the #1 gotcha in
docs/discord-gateway.md.

Protocol (https://discord.com/developers/docs/topics/gateway)
-------------------------------------------------------------
The Discord Gateway is a stateful WebSocket protocol with sequence
numbers and explicit RESUME semantics. Roughly:

  1. Connect to wss://gateway.discord.gg/?v=10&encoding=json
  2. Receive HELLO (op 10) with `heartbeat_interval` ms
  3. Start a heartbeat task: every interval, send HEARTBEAT (op 1)
     with the last sequence number we've seen
  4. Send IDENTIFY (op 2) with token, intents bitmask
  5. Receive READY (op 0, type=READY) with our `session_id` and
     `resume_gateway_url`
  6. Receive DISPATCH events (op 0) — track sequence number, route
     MESSAGE_CREATE through the dispatcher
  7. On disconnect:
     - If we have a session_id and the close code is resumable,
       reconnect to `resume_gateway_url` and send RESUME (op 6) —
       Discord replays missed events
     - Otherwise (close code 4000-4009 or INVALID_SESSION op 9 with
       payload=false), connect fresh and IDENTIFY again
  8. RECONNECT (op 7) from server: close + reconnect (RESUME path)
  9. INVALID_SESSION (op 9): wait 1-5s, then re-IDENTIFY fresh

Heartbeat ack tracking: server replies HEARTBEAT_ACK (op 11) to every
heartbeat we send. If we send 2 in a row without an ack, the connection
is "zombied" — close it and reconnect.

This implementation is single-shard (no `shard` field in IDENTIFY) —
Discord auto-shards for bots in <2500 guilds, which covers all SRE
bot use cases.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from bot_cmder.adapters.discord.adapter import DiscordAdapter
from bot_cmder.adapters.discord.schemas import MessageCreatePayload
from bot_cmder.core.dispatcher import Dispatcher
from bot_cmder.core.events import IncomingMessage
from bot_cmder.core.redact import redact_text

logger = logging.getLogger(__name__)

# Discord Gateway opcodes (https://discord.com/developers/docs/topics/opcodes-and-status-codes#gateway-gateway-opcodes)
_OP_DISPATCH = 0  # server → client: an event
_OP_HEARTBEAT = 1  # both directions
_OP_IDENTIFY = 2  # client → server: initial auth
_OP_RESUME = 6  # client → server: resume after reconnect
_OP_RECONNECT = 7  # server → client: please reconnect
_OP_INVALID_SESSION = 9  # server → client: session is dead
_OP_HELLO = 10  # server → client: connection accepted, here's the heartbeat interval
_OP_HEARTBEAT_ACK = 11  # server → client: ack of our heartbeat

# Discord Gateway intents bitmask. We only need messages-related
# intents; presence / voice / etc. are skipped to keep bandwidth low
# and to avoid unnecessary "privileged intent" requirements.
_INTENT_GUILD_MESSAGES = 1 << 9  # MESSAGE_CREATE in guild channels
_INTENT_DIRECT_MESSAGES = 1 << 12  # MESSAGE_CREATE in DMs
_INTENT_MESSAGE_CONTENT = 1 << 15  # PRIVILEGED — required to read content
_DEFAULT_INTENTS = _INTENT_GUILD_MESSAGES | _INTENT_DIRECT_MESSAGES | _INTENT_MESSAGE_CONTENT

# Default Gateway URL. Discord recommends fetching the canonical URL
# from /gateway/bot for sharding-aware setups, but for single-shard
# single-instance bots the static URL is documented to work
# indefinitely.
_DEFAULT_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# Resumable close codes — anything in this set means we can RESUME
# rather than re-IDENTIFY (https://discord.com/developers/docs/topics/opcodes-and-status-codes#gateway-gateway-close-event-codes).
# Codes 4000-4009 are the "operationally fine, just reconnect" range;
# 4010+ are fatal (bad shard, intent not enabled, etc.).
_RESUMABLE_CLOSE_CODES = frozenset({1000, 1001, 1006, 4000, 4001, 4002, 4003, 4005, 4007, 4008, 4009})

# Backoff schedule (seconds) for fatal error recovery. Same shape as
# Telegram polling / Slack socket — exponential 1→30s with reset on
# successful (re)connect.
_INITIAL_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 30.0


class DiscordGatewayDaemon:
    """Long-lived WebSocket consumer for Discord Gateway events."""

    def __init__(
        self,
        bot_token: str,
        adapter: DiscordAdapter,
        dispatcher: Dispatcher,
        *,
        intents: int = _DEFAULT_INTENTS,
        gateway_url: str = _DEFAULT_GATEWAY_URL,
        # Test injection point. In tests we pass a function returning
        # an in-memory fake; production uses the real
        # `websockets.connect`.
        connect_ws: Callable[[str], Any] | None = None,
    ) -> None:
        self._token = bot_token
        self._adapter = adapter
        self._dispatcher = dispatcher
        self._intents = intents
        self._gateway_url = gateway_url
        self._connect_ws = connect_ws or self._default_connect_ws

        # Per-connection state. Reset on every fresh IDENTIFY; preserved
        # across RESUME-eligible reconnects.
        self._session_id: str | None = None
        self._resume_url: str | None = None
        self._last_seq: int | None = None
        self._heartbeat_task: asyncio.Task | None = None
        # Counts heartbeats sent without an ack. Reset to 0 on each
        # HEARTBEAT_ACK; if it reaches 2 the connection is considered
        # zombied and we force a reconnect.
        self._heartbeat_unacked = 0

    async def run(self) -> None:
        """Outer loop: connect, IDENTIFY-or-RESUME, stream, repeat."""
        backoff_s = _INITIAL_BACKOFF_S
        while True:
            # Decide which URL to use for this connection. Fresh
            # IDENTIFY uses the canonical URL; RESUME uses the URL
            # Discord handed us in the previous READY event.
            url = self._resume_url if self._session_id else self._gateway_url
            try:
                logger.info("discord gateway: connecting to %s", url)
                await self._stream(url)
                # _stream returning normally means a graceful disconnect
                # (Discord asked us to reconnect via op 7). Backoff
                # was reset on connect, so go straight back around.
                logger.info("discord gateway: graceful disconnect, reconnecting")
                backoff_s = _INITIAL_BACKOFF_S
            except asyncio.CancelledError:
                logger.info("discord gateway daemon shutting down")
                await self._stop_heartbeat()
                raise
            except _SessionInvalidated:
                # Server told us our session is dead (op 9) — wipe
                # state and re-IDENTIFY fresh on the next iteration.
                self._reset_session()
                # Discord docs say wait 1-5s before re-IDENTIFY to
                # avoid a thundering herd; 2s is a fine middle.
                await asyncio.sleep(2)
            except (WebSocketException, OSError) as exc:
                logger.warning("discord gateway error (%s), retrying in %.1fs", exc, backoff_s)
                await self._stop_heartbeat()
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, _MAX_BACKOFF_S)

    # ---- connection lifecycle -----------------------------------------

    async def _stream(self, url: str) -> None:
        """Open one WSS connection, drive it to either disconnect or
        a fatal session-invalidated state."""
        async with self._connect_ws(url) as ws:
            self._heartbeat_unacked = 0
            try:
                async for raw in ws:
                    frame = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                    try:
                        payload = json.loads(frame)
                    except json.JSONDecodeError:
                        logger.warning("discord gateway: non-JSON frame ignored: %r", frame[:200])
                        continue
                    done = await self._handle_payload(ws, payload)
                    if done:
                        return
            except ConnectionClosed as exc:
                if exc.code in _RESUMABLE_CLOSE_CODES:
                    logger.info("discord gateway: resumable close (code=%s)", exc.code)
                    return  # outer loop attempts RESUME
                # Fatal close: wipe session so the outer loop does a
                # fresh IDENTIFY instead of an invalid RESUME.
                logger.warning("discord gateway: fatal close (code=%s, reason=%r)", exc.code, exc.reason)
                self._reset_session()
                raise
            finally:
                await self._stop_heartbeat()

    async def _handle_payload(self, ws: Any, payload: dict[str, Any]) -> bool:
        """Dispatch one Gateway frame. Returns True when the caller
        should treat the connection as done."""
        op = payload.get("op")
        if op == _OP_HELLO:
            interval_ms = payload["d"]["heartbeat_interval"]
            await self._start_heartbeat(ws, interval_ms / 1000.0)
            # On HELLO, decide between IDENTIFY (fresh) and RESUME
            # (have a session_id from a prior connection).
            if self._session_id is not None:
                logger.info("discord gateway: resuming session %s (seq=%s)", self._session_id, self._last_seq)
                await ws.send(
                    json.dumps(
                        {
                            "op": _OP_RESUME,
                            "d": {
                                "token": self._token,
                                "session_id": self._session_id,
                                "seq": self._last_seq,
                            },
                        }
                    )
                )
            else:
                logger.info("discord gateway: identifying (intents=%d)", self._intents)
                await ws.send(
                    json.dumps(
                        {
                            "op": _OP_IDENTIFY,
                            "d": {
                                "token": self._token,
                                "intents": self._intents,
                                "properties": {
                                    "os": "linux",
                                    "browser": "bot-cmder",
                                    "device": "bot-cmder",
                                },
                            },
                        }
                    )
                )
            return False

        if op == _OP_HEARTBEAT_ACK:
            self._heartbeat_unacked = 0
            return False

        if op == _OP_HEARTBEAT:
            # Server can request a heartbeat outside the normal cadence
            # (op 1 from server, not just our op 1 to server). Fire one
            # immediately.
            await ws.send(json.dumps({"op": _OP_HEARTBEAT, "d": self._last_seq}))
            return False

        if op == _OP_RECONNECT:
            logger.info("discord gateway: server requested reconnect")
            return True  # exit _stream; outer loop reconnects (RESUME path)

        if op == _OP_INVALID_SESSION:
            resumable = payload.get("d", False)
            logger.warning("discord gateway: invalid session (resumable=%s)", resumable)
            if not resumable:
                # Server tells us the session is dead beyond hope.
                # Raising here unwinds _stream and reaches the outer
                # loop's _SessionInvalidated handler, which wipes
                # state + sleeps before re-IDENTIFY.
                raise _SessionInvalidated()
            return True

        if op == _OP_DISPATCH:
            seq = payload.get("s")
            if seq is not None:
                self._last_seq = seq
            event_type = payload.get("t")
            event_data = payload.get("d", {})
            if event_type == "READY":
                self._session_id = event_data.get("session_id")
                self._resume_url = event_data.get("resume_gateway_url")
                bot_user = event_data.get("user", {})
                bot_id = bot_user.get("id")
                if bot_id:
                    # Plumb our own bot user ID into the adapter so
                    # it knows which mention IDs to strip from message
                    # content when deciding "is this addressed to me?"
                    self._adapter._bot_user_id = bot_id
                logger.info("discord gateway: READY (bot_id=%s, session=%s)", bot_id, self._session_id)
            elif event_type == "RESUMED":
                logger.info("discord gateway: RESUMED")
            elif event_type == "MESSAGE_CREATE":
                await self._handle_message_create(event_data)
            # Other event types (TYPING_START, GUILD_CREATE on connect,
            # etc.) are unhandled — fine, the dispatcher only cares
            # about MESSAGE_CREATE in this phase.
            return False

        # Unknown opcode — log and continue. Discord may add new ones;
        # ignoring is safer than dying.
        logger.debug("discord gateway: ignoring unknown op %s", op)
        return False

    # ---- heartbeat ----------------------------------------------------

    async def _start_heartbeat(self, ws: Any, interval_s: float) -> None:
        """Spawn the heartbeat-sender task. Cancels any prior heartbeat
        task first (in case _start_heartbeat fires twice without a
        _stop_heartbeat between them — shouldn't happen but defends
        against it)."""
        await self._stop_heartbeat()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws, interval_s))

    async def _stop_heartbeat(self) -> None:
        if self._heartbeat_task is None:
            return
        self._heartbeat_task.cancel()
        # Swallow cancellation OR any exception the heartbeat
        # task raised post-cancel; we just want it stopped.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._heartbeat_task
        self._heartbeat_task = None

    async def _heartbeat_loop(self, ws: Any, interval_s: float) -> None:
        """Send HEARTBEAT every `interval_s`. If two heartbeats in a
        row go unacked, the connection is zombied — close it so the
        outer loop reconnects with RESUME.

        Discord's first heartbeat should be sent after a random
        jitter of `interval * jitter_fraction` (where jitter is in
        [0,1]) per spec, to avoid thundering herd. We use a small
        fixed jitter (0.1) — production-grade clients randomize, but
        a single-instance bot doesn't need that."""
        # Initial small jitter — gives a tiny stagger if multiple
        # bots come up at once, without complicating the test path.
        await asyncio.sleep(interval_s * 0.1)
        while True:
            if self._heartbeat_unacked >= 2:
                logger.warning("discord gateway: 2 heartbeats unacked, forcing reconnect")
                await ws.close(code=4000, reason="zombied connection")
                return
            try:
                await ws.send(json.dumps({"op": _OP_HEARTBEAT, "d": self._last_seq}))
                self._heartbeat_unacked += 1
            except (WebSocketException, ConnectionClosed):
                return
            await asyncio.sleep(interval_s)

    # ---- message dispatch ---------------------------------------------

    async def _handle_message_create(self, data: dict[str, Any]) -> None:
        """Parse + dispatch one MESSAGE_CREATE payload."""
        try:
            mc = MessageCreatePayload.model_validate(data)
        except Exception:
            logger.exception("discord gateway: bad MESSAGE_CREATE payload, dropping")
            return
        msg = self._adapter.parse(mc)
        if msg is None:
            logger.debug("discord gateway: ignored msg id=%s (not addressed to bot)", data.get("id"))
            return
        # Run dispatch in a separate task so a slow handler (SSH 30s)
        # can't block the WS read loop and stop heartbeats. Same
        # backpressure rationale as the Slack socket daemon.
        asyncio.create_task(self._process(msg), name=f"discord-dispatch-{msg.message_id}")

    async def _process(self, msg: IncomingMessage) -> None:
        logger.info("recv  %-22s chat=%s text=%r", msg.user.norm_id, msg.chat_id, redact_text(msg.text))
        try:
            resp = await self._dispatcher.dispatch(msg)
            if resp is None:
                logger.info("no-op %-22s chat=%s (non-command)", msg.user.norm_id, msg.chat_id)
                return
            await self._adapter.send(msg, resp)
            logger.info("sent  %-22s chat=%s bytes=%d", msg.user.norm_id, msg.chat_id, len(resp.text))
        except Exception:
            logger.exception("discord gateway dispatch failed for chat=%s", msg.chat_id)

    # ---- helpers ------------------------------------------------------

    def _reset_session(self) -> None:
        """Wipe state so the next connect does a fresh IDENTIFY."""
        self._session_id = None
        self._resume_url = None
        self._last_seq = None

    def _default_connect_ws(self, url: str) -> Any:
        return websockets.connect(url)


class _SessionInvalidated(Exception):
    """Internal marker — raised when Discord sends INVALID_SESSION
    with resumable=False, signalling the outer loop to wipe state
    and re-IDENTIFY fresh after a brief sleep."""
