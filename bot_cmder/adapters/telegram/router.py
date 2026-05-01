from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, status

from bot_cmder.adapters.telegram.adapter import TelegramAdapter
from bot_cmder.adapters.telegram.schemas import TelegramUpdate
from bot_cmder.core.dispatcher import Dispatcher
from bot_cmder.core.events import IncomingMessage

logger = logging.getLogger(__name__)


def make_router(
    adapter: TelegramAdapter,
    dispatcher: Dispatcher,
    *,
    webhook_secret: str | None = None,
) -> APIRouter:
    """Build a FastAPI router that exposes POST /webhooks/telegram.

    If `webhook_secret` is set, the X-Telegram-Bot-Api-Secret-Token
    header is verified with constant-time comparison. The actual
    dispatch runs in a BackgroundTask so the webhook can return 200
    well within Telegram's timeout window.
    """
    router = APIRouter()

    @router.post("/webhooks/telegram")
    async def telegram_webhook(
        update: TelegramUpdate,
        background: BackgroundTasks,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> dict[str, bool]:
        if webhook_secret is not None:
            received = x_telegram_bot_api_secret_token or ""
            if not hmac.compare_digest(received, webhook_secret):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad webhook secret")

        msg = adapter.parse(update)
        if msg is None:
            return {"ok": True}

        background.add_task(_process, adapter, dispatcher, msg)
        return {"ok": True}

    return router


async def _process(adapter: TelegramAdapter, dispatcher: Dispatcher, msg: IncomingMessage) -> None:
    logger.info("recv  %-22s chat=%s text=%r", msg.user.norm_id, msg.chat_id, msg.text)
    try:
        resp = await dispatcher.dispatch(msg)
        if resp is None:
            logger.info("no-op %-22s chat=%s (non-command)", msg.user.norm_id, msg.chat_id)
            return
        await adapter.send(msg, resp)
        logger.info("sent  %-22s chat=%s bytes=%d", msg.user.norm_id, msg.chat_id, len(resp.text))
    except Exception:
        logger.exception("telegram dispatch failed for chat=%s", msg.chat_id)
