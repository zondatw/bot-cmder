"""Tests for `bot_cmder.cli.slack_manifest`.

Two halves:

  - URL resolution precedence (CLI flag > SLACK_REQUEST_URL env >
    NGROK_DOMAIN env > error). Same precedence pattern as
    register_discord_commands._resolve_guild, so the contract is
    pinned the same way.
  - Manifest shape — pulled from a hand-built CommandRegistry so the
    test isn't coupled to whatever builtins happen to be installed
    in app_config; we verify the SLASH command count, the URL is
    propagated correctly, and the schema matches Slack's required
    keys.
"""

from __future__ import annotations

import argparse

import pytest

from bot_cmder.cli.slack_manifest import (
    WEBHOOK_PATH,
    _normalize_request_url,
    _resolve_request_url,
    build_manifest,
)
from bot_cmder.config.settings import Settings, get_settings
from bot_cmder.core.events import OutgoingResponse, ResponseKind
from bot_cmder.core.registry import CommandRegistry, Risk, Router, register


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """Same isolation trick as test_discord_register — stop
    the on-disk .env from bleeding env values into precedence tests."""
    get_settings.cache_clear()
    monkeypatch.setattr(
        "bot_cmder.cli.slack_manifest.get_settings",
        lambda: Settings(_env_file=None),
    )
    yield
    get_settings.cache_clear()


def _ns(**kwargs) -> argparse.Namespace:
    defaults = {"request_url": None}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# --- URL normalization -------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("my-tunnel.ngrok-free.dev", f"https://my-tunnel.ngrok-free.dev{WEBHOOK_PATH}"),
        ("https://my-tunnel.ngrok-free.dev", f"https://my-tunnel.ngrok-free.dev{WEBHOOK_PATH}"),
        (f"https://my-tunnel.ngrok-free.dev{WEBHOOK_PATH}", f"https://my-tunnel.ngrok-free.dev{WEBHOOK_PATH}"),
        ("  https://my-tunnel.ngrok-free.dev/  ", f"https://my-tunnel.ngrok-free.dev{WEBHOOK_PATH}"),
    ],
)
def test_normalize_request_url(raw, expected):
    assert _normalize_request_url(raw) == expected


# --- URL resolution precedence ----------------------------------------


def test_cli_flag_wins_over_env(monkeypatch):
    monkeypatch.setenv("SLACK_REQUEST_URL", "from-env.example")
    monkeypatch.setenv("NGROK_DOMAIN", "from-ngrok.example")
    assert _resolve_request_url(_ns(request_url="from-cli.example")) == f"https://from-cli.example{WEBHOOK_PATH}"


def test_env_used_when_no_flag(monkeypatch):
    monkeypatch.setenv("SLACK_REQUEST_URL", "from-env.example")
    monkeypatch.setenv("NGROK_DOMAIN", "from-ngrok.example")
    assert _resolve_request_url(_ns()) == f"https://from-env.example{WEBHOOK_PATH}"


def test_ngrok_fallback_when_only_ngrok_set(monkeypatch):
    monkeypatch.delenv("SLACK_REQUEST_URL", raising=False)
    monkeypatch.setenv("NGROK_DOMAIN", "from-ngrok.example")
    assert _resolve_request_url(_ns()) == f"https://from-ngrok.example{WEBHOOK_PATH}"


def test_no_url_anywhere_returns_none(monkeypatch):
    """Caller (the script's main) prints a helpful error and exits 1."""
    monkeypatch.delenv("SLACK_REQUEST_URL", raising=False)
    monkeypatch.delenv("NGROK_DOMAIN", raising=False)
    assert _resolve_request_url(_ns()) is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_env_treated_as_unset(monkeypatch, blank):
    """`SLACK_REQUEST_URL=` (placeholder line) must fall through to
    NGROK_DOMAIN, not produce a manifest with empty URL."""
    monkeypatch.setenv("SLACK_REQUEST_URL", blank)
    monkeypatch.setenv("NGROK_DOMAIN", "from-ngrok.example")
    assert _resolve_request_url(_ns()) == f"https://from-ngrok.example{WEBHOOK_PATH}"


# --- Manifest shape ----------------------------------------------------


def _toy_registry() -> CommandRegistry:
    """Build a tiny registry with one Command + one Router so the
    manifest test isn't coupled to the actual builtins (which churn)."""
    registry = CommandRegistry()

    @register("ping", risk=Risk.SAFE, description="Reply pong", registry=registry)
    async def _ping(ctx, args):
        return OutgoingResponse(kind=ResponseKind.TEXT, text="pong")

    router = Router(name="svc", description="Service ops")

    @router.subcommand("status", description="Status")
    async def _status(ctx, args):
        return OutgoingResponse(kind=ResponseKind.TEXT, text="up")

    registry.register_router(router)
    return registry


URL = f"https://test.example{WEBHOOK_PATH}"


def test_manifest_has_one_entry_per_command_plus_router():
    manifest = build_manifest(_toy_registry(), request_url=URL)
    cmds = manifest["features"]["slash_commands"]
    names = sorted(c["command"] for c in cmds)
    assert names == ["/ping", "/svc"]


def test_manifest_propagates_request_url_to_every_entry():
    manifest = build_manifest(_toy_registry(), request_url=URL)
    cmds = manifest["features"]["slash_commands"]
    assert all(c["url"] == URL for c in cmds)
    assert manifest["settings"]["event_subscriptions"]["request_url"] == URL


def test_manifest_includes_required_oauth_scopes():
    """commands is required for any /cmd to fire; chat:write reserved
    for Phase 6 Block Kit posting (declaring it now avoids a reinstall)."""
    manifest = build_manifest(_toy_registry(), request_url=URL)
    scopes = set(manifest["oauth_config"]["scopes"]["bot"])
    assert "commands" in scopes
    assert "chat:write" in scopes


def test_manifest_router_description_lists_subcommand_count():
    manifest = build_manifest(_toy_registry(), request_url=URL)
    cmds = manifest["features"]["slash_commands"]
    svc = next(c for c in cmds if c["command"] == "/svc")
    assert "1 subcommands" in svc["description"]
    assert "/svc help" in svc["description"]


def test_manifest_truncates_overly_long_description():
    """Slack rejects manifest descriptions >2000 chars; trim defensively."""
    registry = CommandRegistry()

    @register("bigdesc", risk=Risk.SAFE, description="x" * 5000, registry=registry)
    async def _h(ctx, args):
        return OutgoingResponse(kind=ResponseKind.TEXT, text="ok")

    manifest = build_manifest(registry, request_url=URL)
    bigdesc = next(c for c in manifest["features"]["slash_commands"] if c["command"] == "/bigdesc")
    assert len(bigdesc["description"]) <= 2000


def test_manifest_explicitly_disables_phase6_features():
    """socket_mode_enabled / org_deploy_enabled / token_rotation_enabled
    must be present in the manifest schema; we keep them all False so
    Phase 5 surface is unambiguous."""
    manifest = build_manifest(_toy_registry(), request_url=URL)
    settings = manifest["settings"]
    assert settings["socket_mode_enabled"] is False
    assert settings["org_deploy_enabled"] is False
    assert settings["token_rotation_enabled"] is False
