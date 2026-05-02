"""POST /webhooks/discord — Discord HTTP Interactions endpoint.

Verifies every incoming request's Ed25519 signature against the
application's public key BEFORE parsing the body. Discord rejects
applications whose endpoint accepts an unsigned payload (it sends
deliberately-bad signatures during the ownership check), so this
isn't optional.

Response model:

  - PING (type 1) → respond {"type": 1} synchronously. Discord uses
    this both to verify endpoint ownership and as a periodic health
    probe.
  - APPLICATION_COMMAND (type 2) → respond {"type": 5} (deferred)
    immediately and schedule the actual dispatch in a BackgroundTask.
    Discord enforces a 3-second cap on the initial response; SSH
    handshakes and OTP gates routinely take longer, so deferring is
    mandatory. The background task PATCHes the @original message
    once dispatch completes.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from bot_cmder.adapters.discord.adapter import DiscordAdapter
from bot_cmder.adapters.discord.schemas import Interaction, InteractionType
from bot_cmder.core.dispatcher import Dispatcher
from bot_cmder.core.events import IncomingMessage

logger = logging.getLogger(__name__)


def make_router(
    adapter: DiscordAdapter,
    dispatcher: Dispatcher,
    *,
    public_key_hex: str,
) -> APIRouter:
    """Build a FastAPI router that exposes POST /webhooks/discord.

    `public_key_hex` is the application's Ed25519 verify key (hex
    string from the Discord developer portal). Required: there is
    no "skip signature verification" mode — Discord refuses to
    onboard endpoints that don't enforce signing.
    """
    try:
        verify_key = VerifyKey(bytes.fromhex(public_key_hex))
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "DISCORD_PUBLIC_KEY is not a valid hex Ed25519 verify key. "
            "Copy the value from the Discord application's General Information page."
        ) from exc

    router = APIRouter()

    @router.post("/webhooks/discord")
    async def discord_webhook(request: Request, background: BackgroundTasks) -> dict:
        body = await request.body()
        signature = request.headers.get("x-signature-ed25519", "")
        timestamp = request.headers.get("x-signature-timestamp", "")
        if not signature or not timestamp:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing signature headers")

        try:
            verify_key.verify(timestamp.encode("utf-8") + body, bytes.fromhex(signature))
        except (BadSignatureError, ValueError) as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad signature") from exc

        interaction = Interaction.model_validate_json(body)

        if interaction.type == InteractionType.PING:
            return {"type": 1}

        if interaction.type != InteractionType.APPLICATION_COMMAND:
            # Autocomplete (4), modal submit (5), message component (3)
            # aren't part of Phase 4. Acknowledge with an ephemeral
            # nope so the user gets a hint instead of a Discord error.
            return {
                "type": 4,
                "data": {"content": "unsupported interaction type", "flags": 64},
            }

        msg = adapter.parse(interaction)
        if msg is None:
            return {"type": 4, "data": {"content": "could not parse command", "flags": 64}}

        # Defer: send {"type": 5} now (within Discord's 3s cap), do the
        # real dispatch in the background, then PATCH @original once
        # we have a reply.
        logger.info(
            "recv  %-22s chat=%s text=%r",
            msg.user.norm_id,
            msg.chat_id,
            msg.text,
        )
        background.add_task(_process, adapter, dispatcher, msg)
        return {"type": 5}

    return router


async def _process(adapter: DiscordAdapter, dispatcher: Dispatcher, msg: IncomingMessage) -> None:
    try:
        resp = await dispatcher.dispatch(msg)
        if resp is None:
            logger.info("no-op %-22s chat=%s (non-command)", msg.user.norm_id, msg.chat_id)
            return
        await adapter.send(msg, resp)
        logger.info(
            "sent  %-22s chat=%s bytes=%d",
            msg.user.norm_id,
            msg.chat_id,
            len(resp.text),
        )
    except Exception:
        logger.exception("discord dispatch failed for chat=%s", msg.chat_id)
