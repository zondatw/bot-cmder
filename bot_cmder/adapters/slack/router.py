"""POST /webhooks/slack — slash command + Events API endpoint.

Two payload shapes share the URL, distinguished by Content-Type:

  - application/x-www-form-urlencoded → slash command. Verify HMAC
    against the raw body, parse to SlashCommandPayload, immediately
    return 200 (Slack's "we got it, working on it"), schedule the
    real dispatch in a BackgroundTask. The background task POSTs the
    actual reply to the per-invocation `response_url` once the
    dispatcher returns. Slack imposes a 3-second cap on the initial
    response, same as Discord — SSH + OTP gate exceed that, so the
    defer-then-respond pattern is mandatory not optional.

  - application/json → Events API. The only event we handle on this
    endpoint is `url_verification` (the one-shot challenge Slack sends
    when you save the Events URL in app config). Everything else is
    accepted and silently dropped: Phase 5 has no @-mention handling.

Signature verify runs BEFORE either branch; Slack rejects endpoints
that accept unsigned payloads, the same way Discord does.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response, status

from bot_cmder.adapters.slack.adapter import SlackAdapter
from bot_cmder.adapters.slack.schemas import SlashCommandPayload, UrlVerificationPayload
from bot_cmder.adapters.slack.signing import SlackSignatureError, verify_slack_signature
from bot_cmder.core.dispatcher import Dispatcher
from bot_cmder.core.events import IncomingMessage
from bot_cmder.core.redact import redact_text

logger = logging.getLogger(__name__)


def make_router(
    adapter: SlackAdapter,
    dispatcher: Dispatcher,
    *,
    signing_secret: str,
) -> APIRouter:
    """Build a FastAPI router exposing POST /webhooks/slack.

    `signing_secret` is the Slack app's "Signing Secret" (Basic
    Information → App Credentials). Required: there is no skip-
    verification mode — Slack refuses to onboard endpoints that
    don't enforce signing.
    """
    router = APIRouter()

    @router.post("/webhooks/slack")
    async def slack_webhook(request: Request, background: BackgroundTasks) -> Response:
        body = await request.body()
        timestamp = request.headers.get("x-slack-request-timestamp", "")
        signature = request.headers.get("x-slack-signature", "")

        try:
            verify_slack_signature(
                signing_secret=signing_secret,
                timestamp=timestamp,
                signature=signature,
                body=body,
            )
        except SlackSignatureError as exc:
            # Slack ignores the response body on 401 — log on our
            # side and return a generic message.
            logger.warning("slack signature rejected: %s", exc)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad signature") from exc

        content_type = request.headers.get("content-type", "")

        # url_verification challenge (Events API). One-shot, never
        # re-fired in normal operation.
        if content_type.startswith("application/json"):
            payload = UrlVerificationPayload.model_validate_json(body)
            if payload.type == "url_verification":
                return Response(content=payload.challenge, media_type="text/plain")
            # Unknown event types: accept silently. Slack retries
            # 4xx but not 200, so quietly succeeding is the right
            # backpressure for events we don't care about.
            return Response(status_code=status.HTTP_200_OK)

        # Slash command (form-encoded). Only the first occurrence of
        # each key is meaningful; parse_qsl returns a flat list which
        # we collapse with `dict()` (later wins, but Slack never
        # sends repeats).
        form_dict = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
        try:
            payload = SlashCommandPayload.model_validate(form_dict)
        except Exception as exc:
            logger.warning("slack slash command parse failed: %s", exc)
            return _ephemeral("could not parse slash command payload")

        msg = adapter.parse(payload)
        if msg is None:
            return _ephemeral("could not parse slash command")

        logger.info(
            "recv  %-22s chat=%s text=%r",
            msg.user.norm_id,
            msg.chat_id,
            redact_text(msg.text),
        )
        background.add_task(_process, adapter, dispatcher, msg)
        # Empty 200 body = Slack shows nothing immediately and waits
        # for the response_url POST. Returning text here would race
        # with the background reply and could create a duplicate
        # message in the channel.
        return Response(status_code=status.HTTP_200_OK)

    return router


def _ephemeral(text: str) -> Response:
    """Inline ephemeral reply (used only for early errors before defer).

    Slack accepts a JSON body on the initial response with the same
    `response_type`/`text` shape `response_url` accepts. We use this
    only for synchronous failures (parse errors) — every successful
    invocation goes through the deferred response_url path.
    """
    import json

    return Response(
        content=json.dumps({"response_type": "ephemeral", "text": text}),
        media_type="application/json",
        status_code=status.HTTP_200_OK,
    )


async def _process(adapter: SlackAdapter, dispatcher: Dispatcher, msg: IncomingMessage) -> None:
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
        logger.exception("slack dispatch failed for chat=%s", msg.chat_id)
