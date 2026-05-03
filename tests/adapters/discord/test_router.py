from __future__ import annotations

import json

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from nacl.signing import SigningKey

from bot_cmder.adapters.discord.adapter import DiscordAdapter
from bot_cmder.adapters.discord.client import DiscordClient
from bot_cmder.adapters.discord.router import make_router
from bot_cmder.audit.log import AuditLogger
from bot_cmder.auth.acl import check_allowed
from bot_cmder.commands.builtin import install_all
from bot_cmder.core.dispatcher import Dispatcher
from bot_cmder.core.registry import CommandRegistry


@pytest.fixture
def signing_pair():
    """A fresh PyNaCl SigningKey + matching public key (hex). The bot
    accepts payloads signed by this key; tests sign with it."""
    sk = SigningKey.generate()
    public_hex = sk.verify_key.encode().hex()
    return sk, public_hex


def _sign(sk: SigningKey, body: bytes, timestamp: str) -> str:
    """Mirrors Discord's signing: hex(Ed25519(timestamp + body))."""
    return sk.sign(timestamp.encode("utf-8") + body).signature.hex()


def _build_app(public_hex: str, app_config) -> tuple[FastAPI, DiscordClient]:
    registry = CommandRegistry()
    audit = AuditLogger("/tmp/_discord_test_audit.jsonl")  # noqa: S108
    install_all(registry, audit=audit, config=app_config)
    dispatcher = Dispatcher(registry=registry, config=app_config, audit=audit, acl_check=check_allowed)
    client = DiscordClient(bot_token="fake", application_id="123")
    adapter = DiscordAdapter(client)
    app = FastAPI()
    app.include_router(make_router(adapter, dispatcher, public_key_hex=public_hex))
    return app, client


def _post(http: TestClient, body: dict, sk: SigningKey, *, ts: str = "1700000000"):
    body_bytes = json.dumps(body).encode("utf-8")
    sig = _sign(sk, body_bytes, ts)
    return http.post(
        "/webhooks/discord",
        content=body_bytes,
        headers={
            "x-signature-ed25519": sig,
            "x-signature-timestamp": ts,
            "content-type": "application/json",
        },
    )


# --- signature verification --------------------------------------------


def test_missing_signature_headers_rejected(signing_pair, app_config):
    sk, public_hex = signing_pair
    app, _ = _build_app(public_hex, app_config)
    with TestClient(app) as http:
        resp = http.post("/webhooks/discord", json={"type": 1})
    assert resp.status_code == 401


def test_bad_signature_rejected(signing_pair, app_config):
    sk, public_hex = signing_pair
    app, _ = _build_app(public_hex, app_config)
    body = json.dumps({"id": "1", "application_id": "2", "type": 1, "token": "t", "version": 1}).encode()
    bad_sig = "00" * 64  # right length, wrong content
    with TestClient(app) as http:
        resp = http.post(
            "/webhooks/discord",
            content=body,
            headers={
                "x-signature-ed25519": bad_sig,
                "x-signature-timestamp": "1700000000",
                "content-type": "application/json",
            },
        )
    assert resp.status_code == 401


def test_ping_with_valid_signature_returns_pong(signing_pair, app_config):
    sk, public_hex = signing_pair
    app, _ = _build_app(public_hex, app_config)
    with TestClient(app) as http:
        resp = _post(http, {"id": "1", "application_id": "2", "type": 1, "token": "t", "version": 1}, sk)
    assert resp.status_code == 200
    assert resp.json() == {"type": 1}


# --- application command flow ------------------------------------------


@respx.mock
def test_application_command_returns_deferred_and_patches_original(signing_pair, app_config):
    """End-to-end: signed APPLICATION_COMMAND → defer (type 5)
    immediately, BackgroundTask runs the dispatcher, real reply
    PATCHes back via the @original webhook URL."""
    sk, public_hex = signing_pair
    # The shared app_config fixture pins Telegram norm_ids; add the
    # Discord caller's norm_id to the same role so /help passes ACL
    # under default_allow_safe.
    app_config.users[0].aliases.append("discord:111")
    patch_route = respx.patch("https://discord.com/api/v10/webhooks/123/tok-help/messages/@original").mock(
        return_value=Response(200, json={"id": "msg"})
    )

    app, _ = _build_app(public_hex, app_config)
    body = {
        "id": "1",
        "application_id": "2",
        "type": 2,
        "token": "tok-help",
        "version": 1,
        "channel_id": "42",
        "user": {"id": "111", "username": "zondatw"},
        "data": {"name": "help", "type": 1},
    }
    with TestClient(app) as http:
        resp = _post(http, body, sk)
    # Immediate response is a defer (type 5).
    assert resp.status_code == 200
    assert resp.json() == {"type": 5}
    # Background task has run by the time TestClient context exits, so
    # the @original PATCH should have happened with the /help reply.
    assert patch_route.called
    payload = json.loads(patch_route.calls.last.request.read())
    assert "Available commands" in payload["content"]


@respx.mock
def test_unsupported_interaction_type_returns_ephemeral_nope(signing_pair, app_config):
    sk, public_hex = signing_pair
    app, _ = _build_app(public_hex, app_config)
    body = {
        "id": "1",
        "application_id": "2",
        "type": 4,  # autocomplete
        "token": "t",
        "version": 1,
    }
    with TestClient(app) as http:
        resp = _post(http, body, sk)
    assert resp.status_code == 200
    body_json = resp.json()
    assert body_json["type"] == 4
    # `flags: 64` = ephemeral (only the invoking user sees it)
    assert body_json["data"]["flags"] == 64
