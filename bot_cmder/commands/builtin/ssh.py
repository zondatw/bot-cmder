"""`/ssh <host> <command...>` — escape hatch for arbitrary remote commands.

PRIVILEGED, so the dispatcher's TOTP gate intercepts it. On top of
that, the per-host `allowed_commands` regex list narrows what can
actually run on each box: a regex must match the joined command
string, otherwise the request is refused before the SSH session
even opens. Hosts with no `allowed_commands` configured deny every
/ssh call by default — no implicit "allow anything" mode.

For day-to-day work, prefer `/service-restart` etc. over `/ssh`.
This command exists for the moment when something is broken in a
way the predefined service actions don't cover.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from bot_cmder.connectors.base import ExecResult
from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import OutgoingResponse
from bot_cmder.core.registry import CommandRegistry, Risk, register

if TYPE_CHECKING:
    from bot_cmder.audit.log import AuditLogger
    from bot_cmder.connectors.ssh import SshConnectorPool


def install(
    registry: CommandRegistry,
    *,
    ssh_pool: SshConnectorPool,
    audit: AuditLogger,
) -> None:
    @register(
        "ssh",
        risk=Risk.PRIVILEGED,
        description="Run a remote command on a configured host (allowlist + TOTP)",
        registry=registry,
        timeout_s=30,
    )
    async def _ssh(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
        if len(args) < 2:
            return OutgoingResponse.text_reply("usage: /ssh <host> <command...>")
        host_name, *cmd_parts = args
        cmd = " ".join(cmd_parts)

        spec = ctx.config.hosts.get(host_name)
        if spec is None:
            audit.log(
                event="SSH_UNKNOWN_HOST",
                user=ctx.user.norm_id,
                chat=ctx.chat_id,
                host=host_name,
            )
            return OutgoingResponse.text_reply(
                f"unknown host: {host_name} (known: {', '.join(sorted(ssh_pool.host_names())) or '<none>'})"
            )

        if not spec.allowed_commands:
            audit.log(
                event="SSH_REFUSED",
                user=ctx.user.norm_id,
                chat=ctx.chat_id,
                host=host_name,
                command=cmd,
                reason="no allowed_commands configured for host",
            )
            return OutgoingResponse.text_reply(f"host {host_name!r} has no allowed_commands configured; refusing")

        if not _matches_allowlist(cmd, spec.allowed_commands):
            audit.log(
                event="SSH_REFUSED",
                user=ctx.user.norm_id,
                chat=ctx.chat_id,
                host=host_name,
                command=cmd,
                reason="no allowlist regex matched",
            )
            return OutgoingResponse.text_reply(f"command not in allowlist for {host_name}; refusing")

        connector = ssh_pool.for_host(host_name)
        result = await connector.execute(
            ["sh", "-c", cmd],
            timeout_s=ctx.config.ssh.command_timeout_s,
            max_output_bytes=ctx.config.ssh.max_output_bytes,
        )
        audit.log(
            event="SSH_EXECUTED",
            user=ctx.user.norm_id,
            chat=ctx.chat_id,
            host=host_name,
            command=cmd,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
        )
        return OutgoingResponse.text_reply(_format(result, host_name, cmd))


def _matches_allowlist(cmd: str, patterns: list[str]) -> bool:
    """True iff `cmd` matches at least one full-string regex in `patterns`."""
    for pattern in patterns:
        try:
            if re.fullmatch(pattern, cmd) is not None:
                return True
        except re.error:
            # Bad regex in config — treat as non-matching, log via the
            # caller's audit on the deny path. Don't crash the bot.
            continue
    return False


def _format(r: ExecResult, host: str, cmd: str) -> str:
    header = f"$ ssh {host} {cmd}\n(exit={r.exit_code}, {r.duration_ms}ms)"
    body = []
    if r.stdout.strip():
        body.append("--- stdout ---")
        body.append(r.stdout.rstrip())
    if r.stderr.strip():
        body.append("--- stderr ---")
        body.append(r.stderr.rstrip())
    if not body:
        body.append("(no output)")
    return header + "\n" + "\n".join(body)
