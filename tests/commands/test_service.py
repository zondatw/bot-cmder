from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot_cmder.audit.log import AuditLogger
from bot_cmder.commands.builtin import service as service_builtin
from bot_cmder.config.schema import AppConfig, HostSpec, ServiceSpec, SshConnectorConfig
from bot_cmder.connectors.base import Connector, ExecResult
from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import Platform, PlatformUser
from bot_cmder.core.registry import CommandRegistry


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


def _setup(
    tmp_path: Path,
    *,
    hosts: dict[str, HostSpec],
    services: dict[str, ServiceSpec],
    canned_per_host: dict[str, ExecResult] | None = None,
) -> tuple:
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLogger(audit_path)
    canned_per_host = canned_per_host or {}
    pool = _FakePool({name: _FakeConnector(target=f"ssh:{name}", canned=canned_per_host.get(name)) for name in hosts})
    cfg = AppConfig(hosts=hosts, services=services, ssh=SshConnectorConfig())
    ctx = CommandContext(
        user=PlatformUser(platform=Platform.TELEGRAM, raw_id="111"),
        platform=Platform.TELEGRAM,
        chat_id="42",
        raw_event={},
        config=cfg,
        now=lambda: datetime.now(timezone.utc),
    )
    reg = CommandRegistry()
    service_builtin.install(reg, ssh_pool=pool, audit=audit)
    return reg, ctx, pool, audit_path


# --- /service_list ------------------------------------------------------


@pytest.mark.asyncio
async def test_list_when_no_services(tmp_path):
    reg, ctx, *_ = _setup(tmp_path, hosts={}, services={})
    resp = await reg.get("service_list").handler(ctx, [])
    assert "no services" in resp.text


@pytest.mark.asyncio
async def test_list_shows_services_hosts_and_actions(tmp_path):
    reg, ctx, *_ = _setup(
        tmp_path,
        hosts={"server-a": HostSpec(address="x", user="u")},
        services={
            "api": ServiceSpec(
                hosts=["server-a"],
                actions={"status": "S", "restart": "R"},
            )
        },
    )
    resp = await reg.get("service_list").handler(ctx, [])
    assert "api" in resp.text
    assert "server-a" in resp.text
    assert "restart" in resp.text and "status" in resp.text


# --- /service_status ----------------------------------------------------


@pytest.mark.asyncio
async def test_status_fans_out_in_parallel_to_every_host(tmp_path):
    hosts = {
        "server-a": HostSpec(address="a", user="u"),
        "server-b": HostSpec(address="b", user="u"),
    }
    services = {
        "api": ServiceSpec(
            hosts=["server-a", "server-b"],
            actions={"status": "sudo systemctl status api.service"},
        )
    }
    reg, ctx, pool, audit_path = _setup(tmp_path, hosts=hosts, services=services)
    resp = await reg.get("service_status").handler(ctx, ["api"])
    # Both hosts called.
    assert pool.for_host("server-a").calls == [["sh", "-c", "sudo systemctl status api.service"]]
    assert pool.for_host("server-b").calls == [["sh", "-c", "sudo systemctl status api.service"]]
    # Reply table shows both.
    assert "server-a" in resp.text and "server-b" in resp.text
    # Audit recorded a fanout.
    assert _read_audit(audit_path)[0]["event"] == "SERVICE_FANOUT"


@pytest.mark.asyncio
async def test_status_includes_first_failure_stderr_inline(tmp_path):
    hosts = {
        "server-a": HostSpec(address="a", user="u"),
        "server-b": HostSpec(address="b", user="u"),
    }
    services = {"api": ServiceSpec(hosts=["server-a", "server-b"], actions={"status": "x"})}
    canned = {
        "server-a": ExecResult(0, "OK\n", "", 5, "ssh:server-a"),
        "server-b": ExecResult(3, "", "Unit api.service not found", 5, "ssh:server-b"),
    }
    reg, ctx, _, _ = _setup(tmp_path, hosts=hosts, services=services, canned_per_host=canned)
    resp = await reg.get("service_status").handler(ctx, ["api"])
    assert "Unit api.service not found" in resp.text
    assert "server-b" in resp.text


@pytest.mark.asyncio
async def test_status_unknown_service(tmp_path):
    reg, ctx, *_ = _setup(tmp_path, hosts={}, services={})
    resp = await reg.get("service_status").handler(ctx, ["ghost"])
    assert "unknown service" in resp.text


@pytest.mark.asyncio
async def test_status_service_without_status_action(tmp_path):
    reg, ctx, *_ = _setup(
        tmp_path,
        hosts={"a": HostSpec(address="a", user="u")},
        services={"api": ServiceSpec(hosts=["a"], actions={"restart": "R"})},
    )
    resp = await reg.get("service_status").handler(ctx, ["api"])
    assert "no 'status' action" in resp.text


# --- /service_restart ---------------------------------------------------


@pytest.mark.asyncio
async def test_restart_requires_explicit_host_flag(tmp_path):
    hosts = {"a": HostSpec(address="a", user="u")}
    services = {"api": ServiceSpec(hosts=["a"], actions={"restart": "R"})}
    reg, ctx, *_ = _setup(tmp_path, hosts=hosts, services=services)
    resp = await reg.get("service_restart").handler(ctx, ["api"])
    assert "usage" in resp.text and "--host" in resp.text
    # Regression: usage string must reference the underscore name,
    # not the legacy hyphenated one (an f-string `/service-{action}`
    # got missed in the rename sweep).
    assert "/service_restart" in resp.text
    assert "/service-restart" not in resp.text


@pytest.mark.asyncio
async def test_restart_rejects_host_not_in_service(tmp_path):
    hosts = {"a": HostSpec(address="a", user="u"), "b": HostSpec(address="b", user="u")}
    services = {"api": ServiceSpec(hosts=["a"], actions={"restart": "R"})}
    reg, ctx, *_ = _setup(tmp_path, hosts=hosts, services=services)
    resp = await reg.get("service_restart").handler(ctx, ["api", "--host", "b"])
    assert "not in service" in resp.text


@pytest.mark.asyncio
async def test_restart_runs_action_on_named_host_and_audits(tmp_path):
    hosts = {"a": HostSpec(address="a", user="u"), "b": HostSpec(address="b", user="u")}
    services = {
        "api": ServiceSpec(
            hosts=["a", "b"],
            actions={"restart": "sudo systemctl restart api.service"},
        )
    }
    reg, ctx, pool, audit_path = _setup(tmp_path, hosts=hosts, services=services)
    resp = await reg.get("service_restart").handler(ctx, ["api", "--host", "b"])
    # Only host "b" got the call.
    assert pool.for_host("a").calls == []
    assert pool.for_host("b").calls == [["sh", "-c", "sudo systemctl restart api.service"]]
    assert "exit=0" in resp.text
    audited = _read_audit(audit_path)[-1]
    assert audited["event"] == "SERVICE_EXECUTED"
    assert audited["service"] == "api"
    assert audited["host"] == "b"
    assert audited["action"] == "restart"


# --- /service_logs ------------------------------------------------------


@pytest.mark.asyncio
async def test_logs_uses_logs_action_template(tmp_path):
    hosts = {"a": HostSpec(address="a", user="u")}
    services = {"api": ServiceSpec(hosts=["a"], actions={"logs": "journalctl -u api -n 100"})}
    reg, ctx, pool, _ = _setup(tmp_path, hosts=hosts, services=services)
    await reg.get("service_logs").handler(ctx, ["api", "--host", "a"])
    assert pool.for_host("a").calls == [["sh", "-c", "journalctl -u api -n 100"]]


# --- risk levels --------------------------------------------------------


def test_service_risk_split():
    pool = _FakePool({})
    reg = CommandRegistry()
    service_builtin.install(reg, ssh_pool=pool, audit=AuditLogger("/tmp/y"))  # noqa: S108
    assert reg.get("service_list").effective_2fa is False
    assert reg.get("service_status").effective_2fa is False
    assert reg.get("service_restart").effective_2fa is True
    assert reg.get("service_logs").effective_2fa is True
