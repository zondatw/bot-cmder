from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from bot_cmder.commands.builtin import kubectl as k
from bot_cmder.config.schema import AppConfig, KubectlConfig
from bot_cmder.connectors.base import Connector, ExecResult
from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import Platform, PlatformUser
from bot_cmder.core.registry import CommandRegistry


@dataclass
class FakeConnector(Connector):
    target: str = "fake"
    canned: ExecResult | None = None
    calls: list[list[str]] = field(default_factory=list)
    last_env: dict[str, str] | None = None

    async def execute(self, argv, *, timeout_s=30, env=None, cwd=None, max_output_bytes=3500):
        self.calls.append(argv)
        self.last_env = env
        return self.canned or ExecResult(exit_code=0, stdout="ok\n", stderr="", duration_ms=10, target=self.target)


def _ctx(allowed=None, kubeconfig=None) -> CommandContext:
    cfg = AppConfig(
        kubectl=KubectlConfig(
            allowed_subcommands=allowed or ["get", "describe"],
            kubeconfig=kubeconfig,
        )
    )
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
    k.install(reg, connector=connector)
    return reg.get("kubectl").handler


async def test_no_args_returns_usage_with_allowlist():
    fake = FakeConnector()
    h = _install(fake)
    resp = await h(_ctx(["get", "logs"]), [])
    assert "usage" in resp.text
    assert "get|logs" in resp.text
    assert fake.calls == []


async def test_subcommand_not_in_allowlist_is_rejected():
    fake = FakeConnector()
    h = _install(fake)
    resp = await h(_ctx(["get"]), ["delete", "pods"])
    assert "not in allowlist" in resp.text
    assert fake.calls == []


async def test_allowed_subcommand_dispatches_to_connector():
    fake = FakeConnector(canned=ExecResult(0, "pod-1\npod-2\n", "", 42, "fake"))
    h = _install(fake)
    resp = await h(_ctx(["get"]), ["get", "pods", "-n", "default"])
    assert fake.calls == [["kubectl", "get", "pods", "-n", "default"]]
    assert "pod-1" in resp.text
    assert "exit=0" in resp.text


async def test_kubeconfig_from_config_passed_via_env(tmp_path):
    fake = FakeConnector()
    h = _install(fake)
    cfg_path = tmp_path / "kubeconfig"
    await h(_ctx(["get"], kubeconfig=cfg_path), ["get", "pods"])
    assert fake.last_env == {"KUBECONFIG": str(cfg_path)}


async def test_no_kubeconfig_means_inherit_env():
    fake = FakeConnector()
    h = _install(fake)
    await h(_ctx(["get"]), ["get", "pods"])
    assert fake.last_env is None


async def test_response_includes_stderr_when_present():
    fake = FakeConnector(canned=ExecResult(1, "", "no such resource", 5, "fake"))
    h = _install(fake)
    resp = await h(_ctx(["get"]), ["get", "ghost"])
    assert "stderr" in resp.text
    assert "no such resource" in resp.text
    assert "exit=1" in resp.text
