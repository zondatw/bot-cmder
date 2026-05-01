from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot_cmder.audit.log import AuditLogger
from bot_cmder.commands.builtin import ssh as ssh_builtin
from bot_cmder.config.schema import AppConfig, HostSpec, SshConnectorConfig
from bot_cmder.connectors.base import Connector, ExecResult
from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import Platform, PlatformUser
from bot_cmder.core.registry import CommandRegistry

# --- in-tree fakes that satisfy the SshConnectorPool / Connector contracts ---


@dataclass
class _FakeConnector(Connector):
    target: str = "fake"
    canned: ExecResult | None = None
    calls: list[list[str]] = field(default_factory=list)

    async def execute(self, argv, *, timeout_s=30, env=None, cwd=None, max_output_bytes=3500):
        self.calls.append(list(argv))
        return self.canned or ExecResult(exit_code=0, stdout="ok\n", stderr="", duration_ms=5, target=self.target)


class _FakePool:
    def __init__(self, connectors: dict[str, _FakeConnector]) -> None:
        self._connectors = connectors

    def host_names(self) -> list[str]:
        return list(self._connectors)

    def for_host(self, name: str) -> _FakeConnector:
        return self._connectors[name]


def _read_audit(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _ctx(hosts: dict[str, HostSpec]) -> CommandContext:
    cfg = AppConfig(hosts=hosts, ssh=SshConnectorConfig())
    return CommandContext(
        user=PlatformUser(platform=Platform.TELEGRAM, raw_id="111"),
        platform=Platform.TELEGRAM,
        chat_id="42",
        raw_event={},
        config=cfg,
        now=lambda: datetime.now(timezone.utc),
    )


def _setup(tmp_path: Path, hosts: dict[str, HostSpec]) -> tuple:
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLogger(audit_path)
    pool = _FakePool({name: _FakeConnector(target=f"ssh:{name}") for name in hosts})
    reg = CommandRegistry()
    ssh_builtin.install(reg, ssh_pool=pool, audit=audit)
    return reg.get("ssh").handler, pool, audit_path


# --- /ssh tests ---------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_when_too_few_args(tmp_path):
    handler, pool, _ = _setup(tmp_path, {})
    resp = await handler(_ctx({}), [])
    assert "usage" in resp.text


@pytest.mark.asyncio
async def test_unknown_host_is_audited_and_rejected(tmp_path):
    handler, pool, audit_path = _setup(tmp_path, {})
    resp = await handler(_ctx({}), ["ghost-host", "true"])
    assert "unknown host" in resp.text
    assert _read_audit(audit_path)[0]["event"] == "SSH_UNKNOWN_HOST"


@pytest.mark.asyncio
async def test_host_without_allowed_commands_refuses_everything(tmp_path):
    hosts = {"server-a": HostSpec(address="x", user="u")}
    handler, pool, audit_path = _setup(tmp_path, hosts)
    resp = await handler(_ctx(hosts), ["server-a", "echo", "hi"])
    assert "no allowed_commands" in resp.text
    assert _read_audit(audit_path)[0]["event"] == "SSH_REFUSED"


@pytest.mark.asyncio
async def test_command_not_matching_allowlist_is_refused(tmp_path):
    hosts = {"server-a": HostSpec(address="x", user="u", allowed_commands=[r"^sudo systemctl status \w+$"])}
    handler, pool, audit_path = _setup(tmp_path, hosts)
    resp = await handler(_ctx(hosts), ["server-a", "rm", "-rf", "/"])
    assert "not in allowlist" in resp.text
    events = _read_audit(audit_path)
    assert events[-1]["event"] == "SSH_REFUSED"
    assert events[-1]["reason"] == "no allowlist regex matched"


@pytest.mark.asyncio
async def test_matching_command_dispatches_to_connector_and_audits(tmp_path):
    hosts = {"server-a": HostSpec(address="x", user="u", allowed_commands=[r"^sudo systemctl status nginx$"])}
    handler, pool, audit_path = _setup(tmp_path, hosts)
    resp = await handler(_ctx(hosts), ["server-a", "sudo", "systemctl", "status", "nginx"])
    assert "exit=0" in resp.text
    [argv] = pool.for_host("server-a").calls
    assert argv == ["sh", "-c", "sudo systemctl status nginx"]
    audited = _read_audit(audit_path)[-1]
    assert audited["event"] == "SSH_EXECUTED"
    assert audited["host"] == "server-a"
    assert audited["command"] == "sudo systemctl status nginx"


@pytest.mark.asyncio
async def test_invalid_regex_in_config_treated_as_non_match_not_crash(tmp_path):
    hosts = {
        "server-a": HostSpec(
            address="x",
            user="u",
            allowed_commands=[r"[unclosed", r"^echo .+$"],  # first is bad
        )
    }
    handler, pool, _ = _setup(tmp_path, hosts)
    # The good regex still matches; the bad one is silently skipped.
    resp = await handler(_ctx(hosts), ["server-a", "echo", "ok"])
    assert "exit=0" in resp.text


def test_ssh_command_is_privileged():
    pool = _FakePool({})
    reg = CommandRegistry()
    ssh_builtin.install(reg, ssh_pool=pool, audit=AuditLogger("/tmp/x"))  # noqa: S108
    cmd = reg.get("ssh")
    assert cmd.effective_2fa is True
