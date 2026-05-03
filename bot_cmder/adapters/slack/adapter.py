"""SlackAdapter — turn Slack slash-command requests into IncomingMessages.

Phase 5 surface mirrors Phase 4 (Discord) closely:

  - Each chat-platform Command and Router is exposed as one Slack
    slash command. The user types `/svc restart api --host gce`; Slack
    sends `command="/svc"`, `text="restart api --host gce"`. We
    rebuild that into `/svc restart api --host gce` so the dispatcher
    sees the same text shape Telegram and Discord produce, which means
    the existing Router rewrite (`service` + `restart` →
    `service_restart` synthetic) Just Works.

  - `response_url` (per-invocation, one-shot, 30-min TTL) is stashed
    in `IncomingMessage.raw` for the background task to PATCH the
    real reply into.

Reply visibility decision lives here in `send()` (not in the
dispatcher) because it's a Slack-only concern: the dispatcher returns
a platform-neutral `OutgoingResponse` annotated with `risk` +
`command_name`, and we apply the operator's `SlackConfig` to map that
to ephemeral vs in_channel.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from bot_cmder.adapters.base import PlatformAdapter
from bot_cmder.adapters.slack.client import SlackClient
from bot_cmder.adapters.slack.schemas import SlashCommandPayload
from bot_cmder.core.events import IncomingMessage, OutgoingResponse, Platform, PlatformUser
from bot_cmder.core.redact import redact_text

if TYPE_CHECKING:
    from bot_cmder.config.schema import SlackConfig


class SlackAdapter(PlatformAdapter):
    platform = Platform.SLACK

    def __init__(self, client: SlackClient, slack_config: SlackConfig) -> None:
        self._client = client
        self._slack_config = slack_config

    @property
    def client(self) -> SlackClient:
        return self._client

    def parse(self, raw: Any) -> IncomingMessage | None:
        payload = raw if isinstance(raw, SlashCommandPayload) else SlashCommandPayload.model_validate(raw)

        # Slack sends `command="/svc"` separate from `text="restart …"`.
        # Rebuild the canonical /cmd args… text so the dispatcher and
        # router rewrite work identically across platforms.
        text = payload.command if not payload.text else f"{payload.command} {payload.text}"

        return IncomingMessage(
            platform=Platform.SLACK,
            user=PlatformUser(
                platform=Platform.SLACK,
                raw_id=payload.user_id,
                handle=payload.user_name,
                display_name=payload.user_name,
            ),
            chat_id=payload.channel_id,
            text=text,
            # Slack slash commands have no per-message ID exposed via
            # the slash-command payload; use the response_url tail as
            # a stable-enough trace key.
            message_id=payload.response_url.rsplit("/", 1)[-1] or None,
            raw=payload.model_dump(),
            received_at=datetime.now(timezone.utc),
        )

    async def send(self, msg: IncomingMessage, resp: OutgoingResponse) -> None:
        response_url = msg.raw.get("response_url") if isinstance(msg.raw, dict) else None
        if not response_url:
            # Without the per-invocation URL we cannot deliver — bail
            # quietly; the operator can still tail the audit log.
            return
        in_channel = self._resolve_in_channel(resp)
        # When the dispatcher (or /otp resume) tells us "the executed
        # command was different from what was typed", echo THAT — see
        # the displayed_command field on OutgoingResponse for why.
        echo_target = resp.displayed_command or msg.text
        body = self._with_command_echo(echo_target, resp.text)
        await self._client.send_response(response_url, body, in_channel=in_channel)

    def _resolve_in_channel(self, resp: OutgoingResponse) -> bool:
        """Apply SlackConfig.reply_visibility + per-command override.

        Precedence:
          1. visibility_overrides[command_name] if present
          2. reply_visibility global default ("by_risk" branches on resp.risk)
          3. Fall back to ephemeral (safer) when risk is unknown
        """
        # Per-command override wins. The override is one of
        # "in_channel" / "ephemeral" (validated in SlackConfig).
        cmd = resp.command_name
        if cmd is not None:
            override = self._slack_config.visibility_overrides.get(cmd)
            if override is not None:
                return override == "in_channel"

        mode = self._slack_config.reply_visibility
        if mode == "in_channel":
            return True
        if mode == "ephemeral":
            return False

        # by_risk: SAFE → broadcast (team learns), PRIVILEGED → private
        # (no SSH-output leaks). When risk is unknown (ACL deny,
        # unknown command, error path before handler ran), default to
        # ephemeral — operator sees their own error, channel doesn't.
        if resp.risk == "safe":
            return True
        return False

    @staticmethod
    def _with_command_echo(typed: str, reply: str) -> str:
        """Prepend the user's typed command to the reply.

        Slack — unlike Telegram and Discord — does NOT echo slash
        commands into channel history, even to the invoker. Without
        this, scrolling back through chat reads as bot soliloquy:
        you see "unknown host: hello" with no context for what was
        asked. Render the command in a blockquote + inline-code so
        it reads as "this is what was typed":

            > `/service restart hello --host gce`

            unknown host: hello (known: gce)

        Sensitive args get redacted — currently `/otp <code>` only,
        which we never want surfaced into chat history (even ephemeral
        Slack messages persist for a while, and a future visibility
        override could flip /otp to in_channel and broadcast the code).
        """
        echo = redact_text(typed)
        if not reply:
            return f"> `{echo}`"
        return f"> `{echo}`\n\n{reply}"
