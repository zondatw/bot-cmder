"""asyncssh-backed Connector for executing commands on remote hosts.

One SshConnector instance per HostSpec; an SshConnectorPool keeps
them keyed by host name so callers (the /service and /ssh builtins)
can look one up by string.

Connection reuse: each SshConnector keeps the last successful
SSHClientConnection alive for `pool_ttl_s` seconds, refreshing on
expiry. Avoids a TCP+SSH handshake per command (which is most of
the latency for a `systemctl status` round-trip) without holding a
stale socket open forever.

Strict host key checking is always on: when HostSpec.known_hosts
is unset, asyncssh falls back to the bot user's ~/.ssh/known_hosts.
We deliberately don't expose a "disable host key check" knob — the
operator has to ssh-keyscan the host into known_hosts as a one-time
setup step. TOFU-by-default would make a stolen DNS / hijacked-VLAN
attack invisible.
"""

from __future__ import annotations

import asyncio
import shlex
import time
from contextlib import suppress
from typing import TYPE_CHECKING

import asyncssh

from bot_cmder.connectors.base import Connector, ExecResult

if TYPE_CHECKING:
    from bot_cmder.config.schema import HostSpec, SshConnectorConfig


class SshConnector(Connector):
    """One SSH-reachable host, with a one-slot connection pool."""

    def __init__(self, host_name: str, spec: HostSpec, *, pool_ttl_s: int = 300) -> None:
        self.target = f"ssh:{host_name}"
        self._host_name = host_name
        self._spec = spec
        self._pool_ttl_s = pool_ttl_s
        self._conn: asyncssh.SSHClientConnection | None = None
        self._conn_opened_at: float | None = None
        # Serialize the dial path so two coroutines hitting a cold
        # cache don't both open a connection.
        self._dial_lock = asyncio.Lock()

    async def execute(
        self,
        argv: list[str],
        *,
        timeout_s: int = 30,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        max_output_bytes: int = 3500,
    ) -> ExecResult:
        if not argv:
            raise ValueError("argv must not be empty")
        # Build a remote shell command line. asyncssh.run accepts a
        # string and feeds it through the remote shell. We use string
        # form (vs. raw exec) so things like `sudo systemctl restart`
        # and journalctl piping work without us reimplementing a
        # shell on the wire.
        cmd_str = " ".join(shlex.quote(a) for a in argv)
        if env:
            env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
            cmd_str = f"{env_prefix} {cmd_str}"
        if cwd:
            cmd_str = f"cd {shlex.quote(cwd)} && {cmd_str}"

        started = time.monotonic()
        try:
            conn = await self._get_conn()
        except Exception as exc:  # connection / auth / known_hosts failures
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=f"ssh connect failed: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
                target=self.target,
            )

        try:
            result = await asyncio.wait_for(conn.run(cmd_str, check=False), timeout=timeout_s)
        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - started) * 1000)
            # A stuck remote process can leave the channel half-open;
            # drop the whole connection so the next call dials fresh.
            await self.close()
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=f"timeout after {timeout_s}s",
                duration_ms=duration_ms,
                target=self.target,
            )
        except Exception as exc:
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=f"ssh exec error: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
                target=self.target,
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout, stdout_truncated = _truncate(_to_str(result.stdout), max_output_bytes)
        stderr, stderr_truncated = _truncate(_to_str(result.stderr), max_output_bytes)
        return ExecResult(
            exit_code=result.exit_status if result.exit_status is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            target=self.target,
            truncated=stdout_truncated or stderr_truncated,
        )

    async def _get_conn(self) -> asyncssh.SSHClientConnection:
        async with self._dial_lock:
            now = time.monotonic()
            if (
                self._conn is not None
                and self._conn_opened_at is not None
                and now - self._conn_opened_at < self._pool_ttl_s
            ):
                return self._conn
            await self._close_locked()
            opts: dict = {
                "host": self._spec.address,
                "port": self._spec.port,
                "username": self._spec.user,
            }
            if self._spec.key_path is not None:
                opts["client_keys"] = [str(self._spec.key_path)]
            if self._spec.known_hosts is not None:
                opts["known_hosts"] = str(self._spec.known_hosts)
            # else: asyncssh's default `()` uses ~/.ssh/known_hosts (strict).
            self._conn = await asyncssh.connect(**opts)
            self._conn_opened_at = now
            return self._conn

    async def close(self) -> None:
        async with self._dial_lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        if self._conn is None:
            return
        self._conn.close()
        with suppress(Exception):
            await self._conn.wait_closed()
        self._conn = None
        self._conn_opened_at = None


class SshConnectorPool:
    """Lazy SshConnector per HostSpec, keyed by host name.

    Construction is cheap; the SSH dial doesn't happen until the
    first execute() call against a given host. close_all() is
    called from the FastAPI lifespan teardown so we don't leave
    half-open sockets behind on shutdown.
    """

    def __init__(self, hosts: dict[str, HostSpec], cfg: SshConnectorConfig) -> None:
        self._hosts = hosts
        self._cfg = cfg
        self._connectors: dict[str, SshConnector] = {}

    def host_names(self) -> list[str]:
        return list(self._hosts.keys())

    def for_host(self, name: str) -> SshConnector:
        spec = self._hosts.get(name)
        if spec is None:
            raise KeyError(f"unknown host: {name}")
        if name not in self._connectors:
            self._connectors[name] = SshConnector(name, spec, pool_ttl_s=self._cfg.pool_ttl_s)
        return self._connectors[name]

    async def close_all(self) -> None:
        for connector in list(self._connectors.values()):
            await connector.close()
        self._connectors.clear()


def _to_str(maybe_bytes: object) -> str:
    if isinstance(maybe_bytes, bytes):
        return maybe_bytes.decode("utf-8", errors="replace")
    if maybe_bytes is None:
        return ""
    return str(maybe_bytes)


def _truncate(s: str, limit: int) -> tuple[str, bool]:
    encoded = s.encode("utf-8")
    if len(encoded) <= limit:
        return s, False
    head = encoded[:limit].decode("utf-8", errors="replace")
    return head + f"\n[...truncated {len(encoded) - limit} bytes]", True
