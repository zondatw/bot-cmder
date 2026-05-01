"""`/service-*` — predefined ops actions against ServiceSpec hosts.

Four registered commands:

  - /service-list                  (SAFE) — show every configured service
  - /service-status <name>         (SAFE) — fan out the `status` action
                                              to every host in parallel
  - /service-restart <name> --host X  (PRIVILEGED) — TOTP-gated, single
                                                       host only
  - /service-logs    <name> --host X  (PRIVILEGED) — TOTP-gated, single
                                                       host only

Why split into four registered commands instead of one /service with
subcommands: each `Command` carries one Risk level, and we want the
read paths to skip the OTP gate while the write paths require it.
The dispatcher already keys risk by command name, so distinct names
is the natural fit.

Why /service-restart and /service-logs require --host: an implicit
"do this everywhere at once" is the wrong default for a chat-driven
SRE tool. The operator types the host they want; if they want to
hit several hosts they type several /service-restart commands (and
the audit log records each one).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from bot_cmder.connectors.base import ExecResult
from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import OutgoingResponse
from bot_cmder.core.registry import CommandRegistry, Risk, register

if TYPE_CHECKING:
    from bot_cmder.audit.log import AuditLogger
    from bot_cmder.config.schema import ServiceSpec
    from bot_cmder.connectors.ssh import SshConnectorPool


def install(
    registry: CommandRegistry,
    *,
    ssh_pool: SshConnectorPool,
    audit: AuditLogger,
) -> None:
    @register(
        "service-list",
        risk=Risk.SAFE,
        description="List configured services and their hosts",
        registry=registry,
    )
    async def _list(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
        services = ctx.config.services
        if not services:
            return OutgoingResponse.text_reply("no services configured")
        lines = [f"Services ({len(services)}):"]
        for name, spec in sorted(services.items()):
            actions = ", ".join(sorted(spec.actions.keys())) or "<none>"
            hosts = ", ".join(spec.hosts) or "<none>"
            lines.append(f"  {name}")
            lines.append(f"    hosts:   {hosts}")
            lines.append(f"    actions: {actions}")
        return OutgoingResponse.text_reply("\n".join(lines))

    @register(
        "service-status",
        risk=Risk.SAFE,
        description="Run the service's `status` action across every host in parallel",
        registry=registry,
        timeout_s=60,
    )
    async def _status(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
        if len(args) != 1:
            return OutgoingResponse.text_reply("usage: /service-status <name>")
        return await _run_fan_out(ctx, args[0], action="status", ssh_pool=ssh_pool, audit=audit)

    @register(
        "service-restart",
        risk=Risk.PRIVILEGED,
        description="Run the service's `restart` action on ONE host (--host X)",
        registry=registry,
        timeout_s=60,
    )
    async def _restart(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
        return await _run_single_host(ctx, args, action="restart", ssh_pool=ssh_pool, audit=audit)

    @register(
        "service-logs",
        risk=Risk.PRIVILEGED,
        description="Run the service's `logs` action on ONE host (--host X)",
        registry=registry,
        timeout_s=60,
    )
    async def _logs(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
        return await _run_single_host(ctx, args, action="logs", ssh_pool=ssh_pool, audit=audit)


# --- helpers ------------------------------------------------------------


def _parse_host_flag(args: list[str]) -> tuple[list[str], str | None]:
    """Pull `--host X` out of args. Returns (remaining_args, host_or_None)."""
    out: list[str] = []
    host: str | None = None
    i = 0
    while i < len(args):
        if args[i] == "--host" and i + 1 < len(args):
            host = args[i + 1]
            i += 2
            continue
        out.append(args[i])
        i += 1
    return out, host


def _resolve_action(
    ctx: CommandContext, service_name: str, action: str
) -> tuple[ServiceSpec | None, str | None, str | None]:
    """(spec, action_command, error_message) — error_message is set when
    the service or action isn't configured."""
    spec = ctx.config.services.get(service_name)
    if spec is None:
        known = ", ".join(sorted(ctx.config.services)) or "<none>"
        return None, None, f"unknown service: {service_name} (known: {known})"
    cmd = spec.actions.get(action)
    if cmd is None:
        known = ", ".join(sorted(spec.actions)) or "<none>"
        return spec, None, f"service {service_name!r} has no '{action}' action (has: {known})"
    return spec, cmd, None


async def _run_fan_out(
    ctx: CommandContext,
    service_name: str,
    *,
    action: str,
    ssh_pool: SshConnectorPool,
    audit: AuditLogger,
) -> OutgoingResponse:
    spec, action_cmd, err = _resolve_action(ctx, service_name, action)
    if err is not None:
        return OutgoingResponse.text_reply(err)
    assert spec is not None and action_cmd is not None  # for type narrowing

    if not spec.hosts:
        return OutgoingResponse.text_reply(f"service {service_name!r} has no hosts configured")

    audit.log(
        event="SERVICE_FANOUT",
        user=ctx.user.norm_id,
        chat=ctx.chat_id,
        service=service_name,
        action=action,
        hosts=spec.hosts,
    )

    async def _run_one(host_name: str) -> tuple[str, ExecResult | str]:
        if host_name not in ctx.config.hosts:
            return host_name, f"unknown host in service config: {host_name}"
        connector = ssh_pool.for_host(host_name)
        result = await connector.execute(
            ["sh", "-c", action_cmd],
            timeout_s=ctx.config.ssh.command_timeout_s,
            max_output_bytes=ctx.config.ssh.max_output_bytes,
        )
        return host_name, result

    results = await asyncio.gather(*(_run_one(h) for h in spec.hosts))
    return OutgoingResponse.text_reply(_format_fanout(service_name, action, results))


async def _run_single_host(
    ctx: CommandContext,
    args: list[str],
    *,
    action: str,
    ssh_pool: SshConnectorPool,
    audit: AuditLogger,
) -> OutgoingResponse:
    positional, host = _parse_host_flag(args)
    if len(positional) != 1 or host is None:
        return OutgoingResponse.text_reply(f"usage: /service-{action} <name> --host <host>")
    service_name = positional[0]

    spec, action_cmd, err = _resolve_action(ctx, service_name, action)
    if err is not None:
        return OutgoingResponse.text_reply(err)
    assert spec is not None and action_cmd is not None

    if host not in spec.hosts:
        return OutgoingResponse.text_reply(
            f"host {host!r} is not in service {service_name!r}'s hosts ({', '.join(spec.hosts)})"
        )
    if host not in ctx.config.hosts:
        return OutgoingResponse.text_reply(f"unknown host in config.hosts: {host}")

    connector = ssh_pool.for_host(host)
    result = await connector.execute(
        ["sh", "-c", action_cmd],
        timeout_s=ctx.config.ssh.command_timeout_s,
        max_output_bytes=ctx.config.ssh.max_output_bytes,
    )
    audit.log(
        event="SERVICE_EXECUTED",
        user=ctx.user.norm_id,
        chat=ctx.chat_id,
        service=service_name,
        action=action,
        host=host,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
    )
    return OutgoingResponse.text_reply(_format_single(service_name, action, host, action_cmd, result))


# --- formatters ---------------------------------------------------------


def _short_status(r: ExecResult | str) -> str:
    if isinstance(r, str):
        return f"ERR ({r})"
    if r.exit_code == 0:
        return f"OK   ({r.duration_ms}ms)"
    if r.exit_code == -1:
        return f"FAIL ({r.stderr.strip().splitlines()[0] if r.stderr.strip() else 'no output'})"
    return f"EXIT={r.exit_code} ({r.duration_ms}ms)"


def _format_fanout(service_name: str, action: str, results: list[tuple[str, ExecResult | str]]) -> str:
    name_w = max((len(h) for h, _ in results), default=4)
    lines = [f"{service_name} {action}:"]
    for host, r in results:
        lines.append(f"  {host.ljust(name_w)}  {_short_status(r)}")
    # Append details from the first non-OK result if any, so failures
    # don't require a follow-up command to investigate.
    for host, r in results:
        if isinstance(r, ExecResult) and r.exit_code != 0:
            lines.append("")
            lines.append(f"--- {host}: stderr ---")
            lines.append(r.stderr.rstrip() or "(empty)")
            break
    return "\n".join(lines)


def _format_single(service_name: str, action: str, host: str, cmd: str, r: ExecResult) -> str:
    header = f"$ {service_name} {action} on {host}\n" f"  ({cmd})\n" f"  exit={r.exit_code}, {r.duration_ms}ms"
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
