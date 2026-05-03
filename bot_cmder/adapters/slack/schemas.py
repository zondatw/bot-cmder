"""Pydantic models for Slack inbound payloads.

Slack delivers two shapes to a single endpoint, distinguished by
Content-Type:

  - application/x-www-form-urlencoded — slash commands. Body is a
    flat key=value form (token, user_id, command, text, response_url,
    channel_id, ...). FastAPI's `Form()` would handle these but
    parsing manually keeps the signature-verify-then-parse order
    intact (we need the raw body bytes for HMAC anyway).

  - application/json — Events API + url_verification challenges. The
    only event we need to handle on this endpoint right now is the
    one-shot `url_verification` Slack sends when you save the Events
    API URL in app config. Everything else (message events, mentions)
    is out of scope for the slash-command-only Phase 5; the router
    accepts and silently ignores them.

We intentionally don't model every field Slack sends — only what
the dispatcher needs. Extra keys are dropped, not validated, so a
Slack payload schema bump won't break us.
"""

from __future__ import annotations

from pydantic import BaseModel


class SlashCommandPayload(BaseModel):
    """One slash-command invocation, parsed from a form-encoded body."""

    # `command` includes the leading slash, e.g. "/help".
    command: str
    # `text` is everything typed AFTER the command name, raw — we
    # never trust Slack to split args sensibly.
    text: str = ""
    # User who invoked the command (workspace-local ID like "U0123ABCD").
    user_id: str
    # Optional handle. Slack stopped sending `user_name` in some
    # contexts (slash commands in DMs, post-2022); tolerate absence.
    user_name: str | None = None
    # Channel the command was issued in. For DMs to the bot this is
    # the user's DM channel ID, not the user ID.
    channel_id: str
    # Per-invocation reply URL — POST a JSON body here within 30 min
    # to deliver the actual response. Cannot be reused.
    response_url: str
    # Workspace ("team") ID. Recorded for audit but not used for ACL.
    team_id: str | None = None
    # Slack's own per-request trigger ID (used for opening modals,
    # which we don't do — kept for raw-payload completeness).
    trigger_id: str | None = None


class UrlVerificationPayload(BaseModel):
    """The one-shot challenge Slack sends when you save the Events URL."""

    type: str
    challenge: str
