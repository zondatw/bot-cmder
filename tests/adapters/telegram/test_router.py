from __future__ import annotations

import asyncio

import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from bot_cmder.adapters.telegram.adapter import TelegramAdapter
from bot_cmder.adapters.telegram.client import TelegramClient
from bot_cmder.adapters.telegram.router import make_router
from bot_cmder.audit.log import AuditLogger
from bot_cmder.auth.acl import check_allowed
from bot_cmder.commands.builtin import install_all
from bot_cmder.core.dispatcher import Dispatcher
from bot_cmder.core.registry import CommandRegistry


def _make_app(app_config, audit: AuditLogger, *, secret: str | None = None) -> tuple[FastAPI, TelegramClient]:
    registry = CommandRegistry()
    install_all(registry)
    client = TelegramClient(token="fake")
    adapter = TelegramAdapter(client)
    dispatcher = Dispatcher(
        registry=registry,
        config=app_config,
        audit=audit,
        acl_check=check_allowed,
    )
    app = FastAPI()
    app.include_router(make_router(adapter, dispatcher, webhook_secret=secret))
    return app, client


def _payload(text: str = "/help", chat_id: int = 42, user_id: int = 111) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "date": 1700000000,
            "chat": {"id": chat_id, "type": "private", "first_name": "X"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Z", "username": "zondatw"},
            "text": text,
        },
    }


@respx.mock
def test_webhook_accepts_and_dispatches(app_config, audit, audit_path):
    sent = respx.post("https://api.telegram.org/botfake/sendMessage").mock(
        return_value=Response(200, json={"ok": True, "result": {}})
    )
    app, client = _make_app(app_config, audit)
    with TestClient(app) as http:
        resp = http.post("/webhooks/telegram", json=_payload("/help"))
    assert resp.status_code == 200
    assert sent.called
    body = sent.calls.last.request.read().decode()
    assert "Available commands" in body
    asyncio.get_event_loop().run_until_complete(client.aclose())


@respx.mock
def test_webhook_rejects_bad_secret(app_config, audit):
    app, client = _make_app(app_config, audit, secret="topsecret")
    with TestClient(app) as http:
        bad = http.post(
            "/webhooks/telegram",
            json=_payload(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        ok = http.post(
            "/webhooks/telegram",
            json=_payload(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "topsecret"},
        )
    assert bad.status_code == 401
    assert ok.status_code == 200
    asyncio.get_event_loop().run_until_complete(client.aclose())


@respx.mock
def test_unknown_user_is_rejected_with_forbidden(app_config, audit, audit_path):
    sent = respx.post("https://api.telegram.org/botfake/sendMessage").mock(
        return_value=Response(200, json={"ok": True, "result": {}})
    )
    app, client = _make_app(app_config, audit)
    with TestClient(app) as http:
        resp = http.post("/webhooks/telegram", json=_payload("/help", user_id=9999))
    assert resp.status_code == 200
    assert sent.called
    body = sent.calls.last.request.read().decode()
    assert "forbidden" in body
    asyncio.get_event_loop().run_until_complete(client.aclose())
