from __future__ import annotations

from datetime import datetime, timezone

import pytest
import respx
from httpx import Response

from bot_cmder.commands.builtin.health import format_table, install
from bot_cmder.config.schema import AppConfig, HealthcheckConfig, HealthTarget
from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import Platform, PlatformUser
from bot_cmder.core.registry import CommandRegistry


def _ctx(config: AppConfig) -> CommandContext:
    user = PlatformUser(platform=Platform.TELEGRAM, raw_id="111")
    return CommandContext(
        user=user,
        platform=Platform.TELEGRAM,
        chat_id="42",
        raw_event={},
        config=config,
        now=lambda: datetime.now(timezone.utc),
    )


def _config_with_targets(targets: list[HealthTarget]) -> AppConfig:
    return AppConfig(healthcheck=HealthcheckConfig(targets=targets))


def _handler(reg: CommandRegistry):
    cmd = reg.get("health")
    assert cmd is not None
    return cmd.handler


@pytest.mark.asyncio
@respx.mock
async def test_ok_response_classifies_as_ok():
    respx.get("https://api.example.com/healthz").mock(return_value=Response(200))
    cfg = _config_with_targets([HealthTarget(name="api", url="https://api.example.com/healthz")])
    reg = CommandRegistry()
    install(reg)
    resp = await _handler(reg)(_ctx(cfg), [])
    assert "OK" in resp.text
    assert "200" in resp.text


@pytest.mark.asyncio
@respx.mock
async def test_unexpected_status_classifies_as_fail():
    respx.get("https://api.example.com/healthz").mock(return_value=Response(503))
    cfg = _config_with_targets([HealthTarget(name="api", url="https://api.example.com/healthz", expect_status=200)])
    reg = CommandRegistry()
    install(reg)
    resp = await _handler(reg)(_ctx(cfg), [])
    assert "FAIL" in resp.text
    assert "503" in resp.text


@pytest.mark.asyncio
@respx.mock
async def test_connection_error_classifies_as_fail():
    respx.get("https://broken.example.com/healthz").mock(side_effect=ConnectionError("nope"))
    cfg = _config_with_targets([HealthTarget(name="broken", url="https://broken.example.com/healthz")])
    reg = CommandRegistry()
    install(reg)
    resp = await _handler(reg)(_ctx(cfg), [])
    assert "FAIL" in resp.text


@pytest.mark.asyncio
async def test_no_targets_configured_message():
    cfg = _config_with_targets([])
    reg = CommandRegistry()
    install(reg)
    resp = await _handler(reg)(_ctx(cfg), [])
    assert "no healthcheck targets" in resp.text


@pytest.mark.asyncio
@respx.mock
async def test_filters_to_named_targets():
    respx.get("https://a.example.com/h").mock(return_value=Response(200))
    cfg = _config_with_targets(
        [
            HealthTarget(name="a", url="https://a.example.com/h"),
            HealthTarget(name="b", url="https://b.example.com/h"),
        ]
    )
    reg = CommandRegistry()
    install(reg)
    resp = await _handler(reg)(_ctx(cfg), ["a"])
    assert "a " in resp.text
    assert "\nb " not in resp.text


@pytest.mark.asyncio
async def test_unknown_target_name_returns_message():
    cfg = _config_with_targets([HealthTarget(name="a", url="https://a.example.com/h")])
    reg = CommandRegistry()
    install(reg)
    resp = await _handler(reg)(_ctx(cfg), ["zzz"])
    assert "unknown target" in resp.text


def test_format_table_columns():
    from bot_cmder.commands.builtin.health import CheckResult

    out = format_table(
        [
            CheckResult("api", "OK", 200, 12),
            CheckResult("very-long-name", "FAIL", -1, 5000),
        ]
    )
    lines = out.splitlines()
    assert lines[0].startswith("name")
    assert "api" in lines[1]
    assert "very-long-name" in lines[2]
    assert "FAIL" in lines[2]
