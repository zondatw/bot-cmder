from __future__ import annotations

from types import SimpleNamespace

import asyncssh
import pytest

from bot_cmder.config.schema import HostSpec, SshConnectorConfig
from bot_cmder.connectors.ssh import SshConnector, SshConnectorPool, _truncate

# --- _truncate helper ---------------------------------------------------


def test_truncate_passthrough_when_under_limit():
    s, t = _truncate("hello", 100)
    assert s == "hello" and t is False


def test_truncate_marks_when_over_limit():
    s, t = _truncate("a" * 1000, 100)
    assert t is True
    assert "truncated" in s
    assert len(s.encode("utf-8")) <= 100 + 64  # head + marker


# --- SshConnectorPool ---------------------------------------------------


def test_pool_caches_connector_per_host():
    spec = HostSpec(address="x", user="u")
    pool = SshConnectorPool({"server-a": spec, "server-b": spec}, SshConnectorConfig())
    a1 = pool.for_host("server-a")
    a2 = pool.for_host("server-a")
    b = pool.for_host("server-b")
    assert a1 is a2
    assert a1 is not b


def test_pool_unknown_host_raises_keyerror():
    pool = SshConnectorPool({}, SshConnectorConfig())
    with pytest.raises(KeyError):
        pool.for_host("missing")


def test_pool_lists_host_names_in_dict_order():
    spec = HostSpec(address="x", user="u")
    pool = SshConnectorPool({"a": spec, "b": spec, "c": spec}, SshConnectorConfig())
    assert pool.host_names() == ["a", "b", "c"]


# --- SshConnector with mocked asyncssh.connect --------------------------


class _FakeAsyncsshConnection:
    """Stand-in for asyncssh.SSHClientConnection."""

    def __init__(self, *, stdout: str = "", stderr: str = "", exit_status: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status
        self.run_calls: list[str] = []
        self._closed = False

    async def run(self, cmd: str, check: bool = False):
        self.run_calls.append(cmd)
        return SimpleNamespace(stdout=self.stdout, stderr=self.stderr, exit_status=self.exit_status)

    def close(self) -> None:
        self._closed = True

    async def wait_closed(self) -> None:
        pass


@pytest.fixture
def patched_connect(monkeypatch):
    """Replace asyncssh.connect with a factory that returns a configurable fake."""
    fake = _FakeAsyncsshConnection(stdout="hello\n")
    connect_calls: list[dict] = []

    async def _fake_connect(**kwargs):
        connect_calls.append(kwargs)
        return fake

    monkeypatch.setattr(asyncssh, "connect", _fake_connect)
    return SimpleNamespace(fake=fake, calls=connect_calls)


@pytest.mark.asyncio
async def test_execute_ships_shlex_quoted_command(patched_connect):
    spec = HostSpec(address="10.0.0.1", user="deploy")
    c = SshConnector("server-a", spec)
    r = await c.execute(["echo", "hi there"])
    assert r.exit_code == 0
    assert "hello" in r.stdout
    assert r.target == "ssh:server-a"
    # Multi-word arg gets shlex-quoted on the wire.
    assert patched_connect.fake.run_calls == ["echo 'hi there'"]


@pytest.mark.asyncio
async def test_execute_passes_host_address_user_port_to_asyncssh(patched_connect):
    spec = HostSpec(address="10.0.0.1", user="deploy", port=2200)
    c = SshConnector("s", spec)
    await c.execute(["true"])
    [opts] = patched_connect.calls
    assert opts["host"] == "10.0.0.1"
    assert opts["port"] == 2200
    assert opts["username"] == "deploy"


@pytest.mark.asyncio
async def test_execute_includes_key_path_and_known_hosts(tmp_path, patched_connect):
    key = tmp_path / "id_ed25519"
    kh = tmp_path / "known_hosts"
    spec = HostSpec(address="x", user="u", key_path=key, known_hosts=kh)
    c = SshConnector("s", spec)
    await c.execute(["true"])
    [opts] = patched_connect.calls
    assert opts["client_keys"] == [str(key)]
    assert opts["known_hosts"] == str(kh)


@pytest.mark.asyncio
async def test_execute_omits_known_hosts_when_unset_so_asyncssh_uses_default(patched_connect):
    """Regression: known_hosts must default to asyncssh's `()` (system
    file), NOT to None (which disables host key checking entirely).
    Letting it through as None would silently turn off the strict
    checking promise the SshConnector docstring makes."""
    spec = HostSpec(address="x", user="u")  # no known_hosts set
    c = SshConnector("s", spec)
    await c.execute(["true"])
    [opts] = patched_connect.calls
    assert "known_hosts" not in opts, "known_hosts must be unset, not None"


@pytest.mark.asyncio
async def test_execute_returns_failure_result_on_connect_error(monkeypatch):
    async def _boom(**kwargs):
        raise asyncssh.PermissionDenied("auth failed")

    monkeypatch.setattr(asyncssh, "connect", _boom)
    spec = HostSpec(address="x", user="u")
    c = SshConnector("server-a", spec)
    r = await c.execute(["true"])
    assert r.exit_code == -1
    assert "ssh connect failed" in r.stderr
    assert r.target == "ssh:server-a"


@pytest.mark.asyncio
async def test_execute_returns_failure_result_on_exec_exception(monkeypatch):
    class _Bad(_FakeAsyncsshConnection):
        async def run(self, cmd, check=False):
            raise asyncssh.ChannelOpenError(1, "broken")

    bad = _Bad()

    async def _connect(**kwargs):
        return bad

    monkeypatch.setattr(asyncssh, "connect", _connect)
    c = SshConnector("server-a", HostSpec(address="x", user="u"))
    r = await c.execute(["true"])
    assert r.exit_code == -1
    assert "ssh exec error" in r.stderr


@pytest.mark.asyncio
async def test_pool_reuse_within_ttl(patched_connect):
    spec = HostSpec(address="x", user="u")
    c = SshConnector("s", spec, pool_ttl_s=300)
    await c.execute(["true"])
    await c.execute(["true"])
    # asyncssh.connect called only once — connection reused.
    assert len(patched_connect.calls) == 1


@pytest.mark.asyncio
async def test_close_is_idempotent(patched_connect):
    c = SshConnector("s", HostSpec(address="x", user="u"))
    await c.execute(["true"])
    await c.close()
    await c.close()  # second call should not error
