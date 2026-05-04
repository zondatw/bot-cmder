"""SlackSocketDaemon — Socket Mode alternative to the webhook router.

Why this exists (Phase 6b)
--------------------------
The Events API (Phase 5) requires Slack to POST to a public HTTPS
URL the operator owns. For home labs / corp networks / anywhere
outbound is fine but inbound is hard, the bot can instead open a
WebSocket OUT to Slack and have slash-command invocations pushed back
through the open connection. Same dispatcher, same OTP gate, same
audit log; only ingestion differs.

Slack-side mutex
----------------
Socket Mode and Events API are mutually exclusive in the Slack app
config (when Socket Mode is toggled on, the Events API URL field
greys out). So unlike Telegram polling, we don't have to programmatic-
ally tear down the other side at startup — Slack's UI enforces it.
The env-var mode (`SLACK_MODE=socket`) just needs to match what's
configured in the app dashboard, otherwise:
  - SLACK_MODE=socket but Slack app has Socket Mode OFF → 401 from
    apps.connections.open at startup (loud and clear).
  - SLACK_MODE=events but Slack app has Socket Mode ON → Slack drops
    HTTP webhook deliveries silently; operator notices via "no
    `recv` log lines in stderr".

Protocol (https://api.slack.com/apis/connections/socket-implement)
------------------------------------------------------------------
1. POST apps.connections.open with the app token (xapp-...) →
   one-time WSS URL valid ~3 minutes.
2. Connect WebSocket. Receive `{"type":"hello"}` immediately.
3. Per event:
     {"envelope_id": "...", "type": "slash_commands",
      "payload": {... same shape as form-decoded HTTP webhook body ...}}
   Send back `{"envelope_id": "..."}` within 3 seconds (the ACK).
4. `{"type":"disconnect"}` events tell us to reconnect — Slack does
   this for graceful capacity rebalancing AND on errors. Outer loop
   fetches a fresh WSS URL and reconnects.
5. WebSocket pings/pongs are handled by the `websockets` library.

Backoff
-------
Same shape as TelegramDaemon: exponential 1→30s on transient errors,
reset on successful WebSocket connect. CancelledError unwinds cleanly
on FastAPI lifespan shutdown.

Auth failure (401 from apps.connections.open) is logged loudly and
the daemon exits — there's no point retrying a bad token.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from bot_cmder.adapters.slack.adapter import SlackAdapter
from bot_cmder.adapters.slack.schemas import SlashCommandPayload
from bot_cmder.core.dispatcher import Dispatcher
from bot_cmder.core.events import IncomingMessage
from bot_cmder.core.redact import redact_text

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_SLACK_OPEN_URL = "https://slack.com/api/apps.connections.open"
_INITIAL_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 30.0


# Type alias for an "opened WebSocket connection" — anything yielding
# str frames and accepting `send(str)`. Lets tests pass an in-memory
# fake without spinning up a real WSS server. The `websockets` library's
# connect() returns one of these implicitly.
WebSocketLike = Any


class SlackSocketDaemon:
    """Long-lived WebSocket consumer for Slack Socket Mode."""

    def __init__(
        self,
        app_token: str,
        adapter: SlackAdapter,
        dispatcher: Dispatcher,
        *,
        # Allow tests to inject fakes for both the URL fetch and
        # the WebSocket open. Both default to the real implementations
        # against api.slack.com.
        fetch_url: Callable[[], Awaitable[str]] | None = None,
        connect_ws: Callable[[str], Any] | None = None,
    ) -> None:
        self._app_token = app_token
        self._adapter = adapter
        self._dispatcher = dispatcher
        self._http = httpx.AsyncClient(timeout=10.0)
        self._fetch_url = fetch_url or self._default_fetch_url
        self._connect_ws = connect_ws or self._default_connect_ws

    async def aclose(self) -> None:
        await self._http.aclose()

    async def run(self) -> None:
        """Outer loop: fetch WSS URL, stream until disconnect, repeat."""
        backoff_s = _INITIAL_BACKOFF_S
        while True:
            try:
                wss_url = await self._fetch_url()
                # Reset backoff once we have a valid URL — the auth
                # part of the round-trip is the most likely failure.
                backoff_s = _INITIAL_BACKOFF_S
                logger.info("slack socket: connecting to wss URL")
                await self._stream(wss_url)
                # _stream returning normally means Slack sent a
                # graceful "disconnect"; reconnect immediately
                # without backoff.
                logger.info("slack socket: graceful disconnect, reconnecting")
            except asyncio.CancelledError:
                logger.info("slack socket daemon shutting down")
                raise
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    logger.error(
                        "slack socket: %d from apps.connections.open — SLACK_APP_TOKEN "
                        "invalid or missing connections:write scope. Daemon stopping.",
                        exc.response.status_code,
                    )
                    return
                logger.warning(
                    "slack apps.connections.open returned %d, retrying in %.1fs",
                    exc.response.status_code,
                    backoff_s,
                )
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, _MAX_BACKOFF_S)
            except (httpx.HTTPError, OSError, WebSocketException) as exc:
                logger.warning("slack socket failed (%s), retrying in %.1fs", exc, backoff_s)
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, _MAX_BACKOFF_S)

    # ---- defaults (overridable in tests) ------------------------------

    async def _default_fetch_url(self) -> str:
        """POST apps.connections.open with the app token, get WSS URL.

        Slack returns `{"ok": true, "url": "wss://..."}` on success
        (URL valid ~3 minutes; acquired fresh on every reconnect).
        """
        resp = await self._http.post(
            _SLACK_OPEN_URL,
            headers={"Authorization": f"Bearer {self._app_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise httpx.HTTPStatusError(
                f"apps.connections.open returned ok=false ({data.get('error')})",
                request=resp.request,
                response=resp,
            )
        url: str = data["url"]
        return url

    def _default_connect_ws(self, url: str) -> Any:
        """Return the async-context-manager from `websockets.connect`."""
        return websockets.connect(url)

    # ---- streaming + dispatch ------------------------------------------

    async def _stream(self, wss_url: str) -> None:
        """Read WebSocket frames until `disconnect` or the conn drops."""
        async with self._connect_ws(wss_url) as ws:
            async for raw in self._iter_frames(ws):
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("slack socket: non-JSON frame ignored: %r", raw[:200])
                    continue
                done = await self._handle_event(ws, event)
                if done:
                    return

    @staticmethod
    async def _iter_frames(ws: WebSocketLike) -> AsyncIterator[str]:
        """Tiny adapter so `_stream` doesn't care whether the WS yields
        str or bytes (websockets >=14 yields str by default; older / fakes
        may yield bytes). Centralizing the decode here keeps the rest of
        the loop string-pure."""
        try:
            async for frame in ws:
                yield frame.decode("utf-8") if isinstance(frame, bytes) else frame
        except ConnectionClosed:
            return

    async def _handle_event(self, ws: WebSocketLike, event: dict[str, Any]) -> bool:
        """Process one envelope. Returns True when the caller should
        treat the connection as done (graceful disconnect)."""
        ev_type = event.get("type")
        if ev_type == "hello":
            # Connection-established hello — also carries the connection
            # ID. Useful in logs but otherwise nothing to do.
            num = event.get("num_connections", "?")
            logger.info("slack socket: hello (connections=%s)", num)
            return False
        if ev_type == "disconnect":
            logger.info("slack socket: server-requested disconnect (reason=%s)", event.get("reason"))
            return True
        envelope_id = event.get("envelope_id")
        if envelope_id:
            # ACK first (within 3s) — else Slack treats the request as
            # unhandled and may retry. Do this BEFORE any dispatch so
            # a slow handler can't make us miss the deadline.
            await ws.send(json.dumps({"envelope_id": envelope_id}))
        # Only slash_commands envelopes carry the SlashCommandPayload
        # shape we know how to dispatch. Other event types
        # (events_api, interactive) are out of scope for Phase 6b.
        if ev_type != "slash_commands":
            logger.debug("slack socket: ignoring event type=%s", ev_type)
            return False
        payload = event.get("payload", {})
        try:
            spc = SlashCommandPayload.model_validate(payload)
        except Exception:
            logger.exception("slack socket: bad slash_commands payload, dropping")
            return False
        msg = self._adapter.parse(spc)
        if msg is None:
            return False
        # Run dispatch in a task so a slow handler (SSH 30s) doesn't
        # block the WebSocket read loop and keep Slack waiting on
        # subsequent events. Same backpressure rationale as the
        # webhook router's BackgroundTask.
        asyncio.create_task(self._process(msg), name=f"slack-dispatch-{envelope_id}")
        return False

    async def _process(self, msg: IncomingMessage) -> None:
        """Same dispatcher → adapter.send shape the webhook router uses."""
        logger.info("recv  %-22s chat=%s text=%r", msg.user.norm_id, msg.chat_id, redact_text(msg.text))
        try:
            resp = await self._dispatcher.dispatch(msg)
            if resp is None:
                logger.info("no-op %-22s chat=%s (non-command)", msg.user.norm_id, msg.chat_id)
                return
            await self._adapter.send(msg, resp)
            logger.info("sent  %-22s chat=%s bytes=%d", msg.user.norm_id, msg.chat_id, len(resp.text))
        except Exception:
            logger.exception("slack socket dispatch failed for chat=%s", msg.chat_id)
