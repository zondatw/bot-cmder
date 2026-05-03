"""TelegramDaemon — long-polling alternative to the webhook router.

Why this exists (Phase 6a)
--------------------------
The webhook adapter requires a public HTTPS URL Telegram can POST to.
Home labs / corp networks / anything behind NAT or strict egress make
that the friction point. Polling flips the connection direction:
the bot dials OUT to api.telegram.org and Telegram pushes updates
back through the open connection. No inbound port, no domain, no
tunnel. Same dispatcher, same audit log, same OTP gate — only the
ingestion path differs.

Mutex with webhook
------------------
Telegram's API does not allow both modes at once: getUpdates returns
409 Conflict whenever a webhook is registered. The daemon calls
delete_webhook at startup so flipping `TELEGRAM_MODE=polling` Just
Works without a manual deletion step. Operators who want to flip
back to webhook re-run `just set-telegram-bot-webhook` — that
overwrites whatever state we left.

Update offset bookkeeping (in-memory, MVP)
------------------------------------------
Telegram delivers each update with an `update_id`; the next
getUpdates call passes `offset = last_seen_id + 1` to acknowledge
everything before it. We keep this counter in memory only — a bot
restart loses the offset and re-reads up to ~24h of pending updates.
For the SRE-bot use case (commands are inherently retry-safe and
idempotent at the connector level: ACL pass + OTP gate guard
mutating ones; SAFE diagnostics are read-only) this is acceptable.
A future hardening phase can persist the offset to SQLite.

Backoff
-------
Network errors / 5xx from Telegram get exponential backoff capped at
30 s. Successful poll resets the delay. 409 is treated as fatal
during steady-state — it means someone re-registered a webhook out
from under us — and surfaced via a loud log so operators can spot
the misconfiguration. Cancellation (FastAPI lifespan shutdown)
unwinds via asyncio.CancelledError without further retry.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

from bot_cmder.adapters.telegram.adapter import TelegramAdapter
from bot_cmder.adapters.telegram.client import TelegramClient
from bot_cmder.adapters.telegram.schemas import TelegramUpdate
from bot_cmder.core.dispatcher import Dispatcher
from bot_cmder.core.events import IncomingMessage
from bot_cmder.core.redact import redact_text

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Backoff schedule (seconds) for transient errors. Doubles up to a
# 30-second ceiling so we don't hammer Telegram during an outage but
# also don't sit idle for minutes after a brief blip.
_INITIAL_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 30.0


class TelegramDaemon:
    """Long-poll Telegram updates and route each one through the dispatcher."""

    def __init__(
        self,
        client: TelegramClient,
        adapter: TelegramAdapter,
        dispatcher: Dispatcher,
        *,
        long_poll_timeout_s: int = 25,
        drop_pending_on_start: bool = False,
    ) -> None:
        self._client = client
        self._adapter = adapter
        self._dispatcher = dispatcher
        self._long_poll_timeout_s = long_poll_timeout_s
        self._drop_pending_on_start = drop_pending_on_start
        # Updated after every successful poll. None means "first poll
        # ever; let Telegram return whatever it has queued".
        self._next_offset: int | None = None

    async def run(self) -> None:
        """Main loop. Runs until cancelled (FastAPI lifespan shutdown)."""
        await self._prepare_for_polling()
        backoff_s = _INITIAL_BACKOFF_S
        logger.info(
            "telegram daemon polling (timeout=%ds, drop_pending=%s)",
            self._long_poll_timeout_s,
            self._drop_pending_on_start,
        )
        while True:
            try:
                updates = await self._client.get_updates(
                    offset=self._next_offset,
                    timeout_s=self._long_poll_timeout_s,
                    allowed_updates=["message"],
                )
                # Reset backoff after any successful poll, even an
                # empty one — proves the connection is healthy.
                backoff_s = _INITIAL_BACKOFF_S
                await self._handle_updates(updates)
            except asyncio.CancelledError:
                logger.info("telegram daemon shutting down")
                raise
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 409:
                    # Someone re-registered a webhook while we were
                    # running. Surface loudly — the operator must
                    # decide which mode they want; we won't keep
                    # silently re-deleting the webhook every loop.
                    logger.error(
                        "telegram daemon got 409 Conflict — a webhook is now registered. "
                        "Either unset TELEGRAM_MODE=polling or call deleteWebhook. "
                        "Daemon stopping."
                    )
                    return
                logger.warning(
                    "telegram getUpdates returned %d, retrying in %.1fs",
                    exc.response.status_code,
                    backoff_s,
                )
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, _MAX_BACKOFF_S)
            except (httpx.HTTPError, OSError) as exc:
                logger.warning("telegram getUpdates failed (%s), retrying in %.1fs", exc, backoff_s)
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, _MAX_BACKOFF_S)

    async def _prepare_for_polling(self) -> None:
        """Delete any existing webhook so getUpdates is allowed.

        No-op when there's nothing to delete; we still call it
        because the marginal cost is one cheap API call and it's
        the only way to recover automatically from
        webhook-was-set-yesterday states.
        """
        info = await self._client.get_webhook_info()
        existing_url = info.get("url") or ""
        if existing_url:
            logger.warning(
                "telegram had a webhook configured (%s) — deleting so polling can start",
                existing_url,
            )
        await self._client.delete_webhook(drop_pending_updates=self._drop_pending_on_start)

    async def _handle_updates(self, raw_updates: list[dict]) -> None:
        if not raw_updates:
            return
        for raw in raw_updates:
            try:
                update = TelegramUpdate.model_validate(raw)
            except Exception:
                # A malformed update shouldn't take down the whole
                # loop. Log and advance past it so we don't re-poll
                # the same broken update forever.
                logger.exception("telegram daemon could not parse update %r", raw)
                self._advance_offset_past(raw.get("update_id"))
                continue
            self._advance_offset_past(update.update_id)
            msg = self._adapter.parse(update)
            if msg is None:
                logger.debug("telegram daemon ignored update %d (no parse)", update.update_id)
                continue
            await self._process(msg)

    def _advance_offset_past(self, update_id: int | None) -> None:
        """Set offset so the next poll won't return this update again."""
        if update_id is None:
            return
        next_offset = update_id + 1
        if self._next_offset is None or next_offset > self._next_offset:
            self._next_offset = next_offset

    async def _process(self, msg: IncomingMessage) -> None:
        """Run the dispatcher pipeline for one parsed message.

        Same shape as the webhook router's _process — same dispatcher,
        same audit, same send path. Difference is only that we run
        synchronously inside the polling loop instead of as a
        BackgroundTask: there's no 3-second response cap to honor
        when we initiated the request.
        """
        logger.info("recv  %-22s chat=%s text=%r", msg.user.norm_id, msg.chat_id, redact_text(msg.text))
        try:
            resp = await self._dispatcher.dispatch(msg)
            if resp is None:
                logger.info("no-op %-22s chat=%s (non-command)", msg.user.norm_id, msg.chat_id)
                return
            await self._adapter.send(msg, resp)
            logger.info("sent  %-22s chat=%s bytes=%d", msg.user.norm_id, msg.chat_id, len(resp.text))
        except Exception:
            logger.exception("telegram dispatch failed for chat=%s", msg.chat_id)
