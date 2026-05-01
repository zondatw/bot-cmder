from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot_cmder.commands.builtin import runbook as rb
from bot_cmder.config.schema import AppConfig, RunbookConfig
from bot_cmder.connectors.base import Connector, ExecResult
from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import Platform, PlatformUser
from bot_cmder.core.registry import CommandRegistry


@dataclass
class FakeConnector(Connector):
    target: str = "fake"
    calls: list[list[str]] = field(default_factory=list)

    async def execute(self, argv, *, timeout_s=30, env=None, cwd=None, max_output_bytes=3500):
        self.calls.append(argv)
        return ExecResult(0, "done\n", "", 5, self.target)


def _make_runbook(directory: Path, name: str, *, executable: bool = True) -> Path:
    p = directory / name
    p.write_text("#!/bin/sh\necho run\n")
    if executable:
        p.chmod(0o755)
    return p


def _ctx(directory: Path) -> CommandContext:
    cfg = AppConfig(runbook=RunbookConfig(dir=directory))
    return CommandContext(
        user=PlatformUser(platform=Platform.TELEGRAM, raw_id="111"),
        platform=Platform.TELEGRAM,
        chat_id="42",
        raw_event={},
        config=cfg,
        now=lambda: datetime.now(timezone.utc),
    )


def _install(connector):
    reg = CommandRegistry()
    rb.install(reg, connector=connector)
    return reg


@pytest.mark.asyncio
async def test_list_lists_only_executable_files(tmp_path: Path):
    _make_runbook(tmp_path, "ok.sh")
    _make_runbook(tmp_path, "no-x.sh", executable=False)
    hidden = _make_runbook(tmp_path, ".hidden.sh")
    assert os.access(hidden, os.X_OK)  # sanity

    reg = _install(FakeConnector())
    h = reg.get("runbook-list").handler
    resp = await h(_ctx(tmp_path), [])
    assert "ok " in resp.text
    assert "no-x" not in resp.text
    assert ".hidden" not in resp.text


@pytest.mark.asyncio
async def test_list_when_directory_missing(tmp_path: Path):
    reg = _install(FakeConnector())
    h = reg.get("runbook-list").handler
    resp = await h(_ctx(tmp_path / "nonexistent"), [])
    assert "no runbooks" in resp.text


@pytest.mark.asyncio
async def test_run_unknown_name(tmp_path: Path):
    reg = _install(FakeConnector())
    h = reg.get("runbook-run").handler
    resp = await h(_ctx(tmp_path), ["ghost"])
    assert "unknown runbook" in resp.text


@pytest.mark.asyncio
async def test_run_with_path_traversal_name_rejected(tmp_path: Path):
    reg = _install(FakeConnector())
    h = reg.get("runbook-run").handler
    resp = await h(_ctx(tmp_path), ["../etc/passwd"])
    assert "unknown runbook" in resp.text


@pytest.mark.asyncio
async def test_run_with_disallowed_arg_rejected(tmp_path: Path):
    _make_runbook(tmp_path, "ok.sh")
    fake = FakeConnector()
    reg = _install(fake)
    h = reg.get("runbook-run").handler
    resp = await h(_ctx(tmp_path), ["ok", "; rm -rf /"])
    assert "refused" in resp.text
    assert fake.calls == []  # never reached the connector


@pytest.mark.asyncio
async def test_run_invokes_connector_with_argv(tmp_path: Path):
    path = _make_runbook(tmp_path, "deploy.sh")
    fake = FakeConnector()
    reg = _install(fake)
    h = reg.get("runbook-run").handler
    resp = await h(_ctx(tmp_path), ["deploy", "--env=stage", "v1.2.3"])
    assert fake.calls == [[str(path), "--env=stage", "v1.2.3"]]
    assert "exit=0" in resp.text


@pytest.mark.asyncio
async def test_run_no_args_shows_usage(tmp_path: Path):
    reg = _install(FakeConnector())
    h = reg.get("runbook-run").handler
    resp = await h(_ctx(tmp_path), [])
    assert "usage" in resp.text


def test_runbook_run_is_privileged():
    reg = _install(FakeConnector())
    cmd_run = reg.get("runbook-run")
    cmd_list = reg.get("runbook-list")
    assert cmd_run.effective_2fa is True
    assert cmd_list.effective_2fa is False
