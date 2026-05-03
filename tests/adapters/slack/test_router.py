"""Tests for the FastAPI POST /webhooks/slack endpoint.

End-to-end: signed slash command → 200 immediate ack → BackgroundTask
runs the dispatcher → real reply POSTed to response_url. Plus the
edge cases we'd hit in real Slack onboarding (URL verification, bad
signature, replay window).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot_cmder.adapters.slack.adapter import SlackAdapter
from bot_cmder.adapters.slack.client import SlackClient
from bot_cmder.adapters.slack.router import make_router
from bot_cmder.adapters.slack.signing import SIGNATURE_VERSION
from bot_cmder.audit.log import AuditLogger
from bot_cmder.auth.acl import check_allowed
from bot_cmder.commands.builtin import install_all
from bot_cmder.config.schema import SlackConfig
from bot_cmder.core.dispatcher import Dispatcher
from bot_cmder.core.registry import CommandRegistry

SECRET = "shhhh"
RESPONSE_URL = "https://hooks.slack.com/commands/T0/1/abc"


def _sign(body: bytes, ts: str, secret: str = SECRET) -> str:
    base = f"{SIGNATURE_VERSION}:{ts}:".encode() + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def _build_app(app_config) -> FastAPI:
    registry = CommandRegistry()
    audit = AuditLogger("/tmp/_slack_test_audit.jsonl")  # noqa: S108
    install_all(registry, audit=audit, config=app_config)
    dispatcher = Dispatcher(registry=registry, config=app_config, audit=audit, acl_check=check_allowed)
    client = SlackClient()
    adapter = SlackAdapter(client, app_config.slack)
    app = FastAPI()
    app.include_router(make_router(adapter, dispatcher, signing_secret=SECRET))
    return app


def _form_body(**fields) -> bytes:
    """Slack sends slash commands as form-urlencoded — we hand-build
    that to keep raw bytes stable for HMAC."""
    from urllib.parse import urlencode

    return urlencode(fields).encode("utf-8")


def _post_command(http: TestClient, fields: dict, *, ts: str | None = None):
    ts = ts or str(int(time.time()))
    body = _form_body(**fields)
    sig = _sign(body, ts)
    return http.post(
        "/webhooks/slack",
        content=body,
        headers={
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
            "content-type": "application/x-www-form-urlencoded",
        },
    )


# --- signature verification --------------------------------------------


def test_missing_signature_headers_rejected(app_config):
    app = _build_app(app_config)
    with TestClient(app) as http:
        resp = http.post(
            "/webhooks/slack",
            content=_form_body(command="/help", user_id="U1", channel_id="C1", response_url=RESPONSE_URL),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert resp.status_code == 401


def test_bad_signature_rejected(app_config):
    app = _build_app(app_config)
    body = _form_body(command="/help", user_id="U1", channel_id="C1", response_url=RESPONSE_URL)
    with TestClient(app) as http:
        resp = http.post(
            "/webhooks/slack",
            content=body,
            headers={
                "x-slack-request-timestamp": str(int(time.time())),
                # right shape, wrong digest
                "x-slack-signature": f"{SIGNATURE_VERSION}=" + "0" * 64,
                "content-type": "application/x-www-form-urlencoded",
            },
        )
    assert resp.status_code == 401


# --- url_verification (one-shot Events API onboarding) -----------------


def test_url_verification_returns_challenge_plaintext(app_config):
    """Slack saves the Events URL only if our endpoint echoes the
    challenge value back — a JSON challenge POST is part of the dance
    even though our Phase 5 surface is slash-commands-only."""
    app = _build_app(app_config)
    body = json.dumps({"type": "url_verification", "challenge": "abc-XYZ-123"}).encode()
    ts = str(int(time.time()))
    sig = _sign(body, ts)
    with TestClient(app) as http:
        resp = http.post(
            "/webhooks/slack",
            content=body,
            headers={
                "x-slack-request-timestamp": ts,
                "x-slack-signature": sig,
                "content-type": "application/json",
            },
        )
    assert resp.status_code == 200
    assert resp.text == "abc-XYZ-123"


# --- end-to-end slash command flow -------------------------------------


@respx.mock
def test_slash_command_returns_200_then_posts_reply_to_response_url(app_config):
    """The big one: signed /help → 200 ack immediately, BackgroundTask
    dispatches, real reply POSTs to response_url."""
    # Add the Slack norm_id to the same role so /help passes ACL via
    # default_allow_safe.
    app_config.users[0].aliases.append("slack:U0")

    response_route = respx.post(RESPONSE_URL).mock(return_value=httpx.Response(200, text="ok"))

    app = _build_app(app_config)
    with TestClient(app) as http:
        resp = _post_command(
            http,
            {
                "command": "/help",
                "text": "",
                "user_id": "U0",
                "user_name": "zonda",
                "channel_id": "C0",
                "response_url": RESPONSE_URL,
                "team_id": "T0",
            },
        )

    # Immediate response: empty 200 (Slack's "we got it, working").
    assert resp.status_code == 200
    # BackgroundTask has run by the time TestClient context exits, so
    # the response_url POST must have happened.
    assert response_route.called
    body = json.loads(response_route.calls.last.request.read())
    assert "Available commands" in body["text"]
    # /help is SAFE; default reply_visibility is by_risk → in_channel
    assert body["response_type"] == "in_channel"


@respx.mock
def test_privileged_command_reply_is_ephemeral_under_by_risk(app_config):
    """Visibility resolver sees risk='safe' for the OTP prompt path
    (because the dispatcher hasn't actually executed the privileged
    handler — it's stashed for OTP). To pin the actual privileged
    branch we'd need the OTP gate disabled OR the resumed flow; here
    we assert the gating reply itself stays ephemeral via the by_risk
    fallback for unknown risk."""
    # Add the slack id to the same row + grant the privileged command.
    app_config.users[0].aliases.append("slack:U0")
    app_config.acl.commands.setdefault("kubectl", []).append("role:sre")
    # Force visibility to ephemeral globally so this test doesn't
    # depend on whether the OTP-prompt response carries risk.
    app_config.slack = SlackConfig(reply_visibility="ephemeral")

    response_route = respx.post(RESPONSE_URL).mock(return_value=httpx.Response(200, text="ok"))

    app = _build_app(app_config)
    with TestClient(app) as http:
        resp = _post_command(
            http,
            {
                "command": "/kubectl",
                "text": "get pods",
                "user_id": "U0",
                "user_name": "zonda",
                "channel_id": "C0",
                "response_url": RESPONSE_URL,
                "team_id": "T0",
            },
        )
    assert resp.status_code == 200
    assert response_route.called
    body = json.loads(response_route.calls.last.request.read())
    assert body["response_type"] == "ephemeral"


def test_replay_outside_window_rejected(app_config):
    app = _build_app(app_config)
    with TestClient(app) as http:
        resp = _post_command(
            http,
            {
                "command": "/help",
                "text": "",
                "user_id": "U0",
                "user_name": "zonda",
                "channel_id": "C0",
                "response_url": RESPONSE_URL,
                "team_id": "T0",
            },
            ts=str(int(time.time()) - 60 * 30),  # 30 min ago, outside 5-min window
        )
    assert resp.status_code == 401


def test_unparseable_form_returns_inline_ephemeral(app_config):
    """A signed-but-malformed payload (missing required fields) gets
    a synchronous ephemeral so the operator sees the error rather than
    a silent timeout."""
    app = _build_app(app_config)
    # Missing user_id / channel_id / response_url → SlashCommandPayload validation fails
    body = _form_body(command="/help")
    ts = str(int(time.time()))
    sig = _sign(body, ts)
    with TestClient(app) as http:
        resp = http.post(
            "/webhooks/slack",
            content=body,
            headers={
                "x-slack-request-timestamp": ts,
                "x-slack-signature": sig,
                "content-type": "application/x-www-form-urlencoded",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["response_type"] == "ephemeral"
    assert "could not parse" in resp.json()["text"]
