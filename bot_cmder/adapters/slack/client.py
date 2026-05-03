"""Minimal async Slack REST client.

Single purpose for Phase 5: POST a delayed reply to the per-invocation
`response_url` Slack sends with every slash command. That URL is a
self-authorizing one-shot — no `Authorization: Bearer xoxb-...`
needed, which is why this client doesn't take the bot token. (Phase 6
socket mode and any future Block Kit posting via `chat.postMessage`
will need it; we'll plumb it through then.)

Slack's response_url accepts:
  {
    "response_type": "ephemeral" | "in_channel",
    "text": "...",
    "replace_original": true | false
  }

Message text length is capped at 40,000 chars per the Web API limit;
we truncate to a more reasonable 3000 to fit in a chat client without
becoming a wall of scroll. Truncation marker so operators see when
output got clipped.
"""

from __future__ import annotations

from typing import Any

import httpx

# 40,000 is Slack's hard cap; 3000 is our friendlier default. Long
# SSH stdout would blow past either; keeping it tight matches Discord's
# 2000-char habit (operator UX > maximum verbosity).
_SLACK_MAX_REPLY_CHARS = 3000


class SlackClient:
    def __init__(
        self,
        *,
        bot_token: str | None = None,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # bot_token is accepted but unused right now (see module
        # docstring). Storing it keeps the constructor signature
        # stable when Phase 6 starts using it.
        self._bot_token = bot_token
        self._client = httpx.AsyncClient(timeout=timeout_s, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SlackClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def send_response(
        self,
        response_url: str,
        text: str,
        *,
        in_channel: bool,
        replace_original: bool = False,
    ) -> None:
        """POST a delayed reply to a slash command's response_url.

        `in_channel=True` makes everyone in the channel see the reply
        (good for collaborative ops); `False` is ephemeral (only the
        invoker sees it, default-safe for sensitive output).
        """
        body = {
            "response_type": "in_channel" if in_channel else "ephemeral",
            "text": _truncate(text),
            "replace_original": replace_original,
        }
        # Slack returns 200 + "ok" body on success, 4xx on bad URL.
        # We let httpx raise on non-2xx so the background task logs it.
        resp = await self._client.post(response_url, json=body)
        resp.raise_for_status()


def _truncate(content: str) -> str:
    if not content:
        return "(no output)"
    if len(content) <= _SLACK_MAX_REPLY_CHARS:
        return content
    marker = "\n[...truncated]"
    return content[: _SLACK_MAX_REPLY_CHARS - len(marker)] + marker
