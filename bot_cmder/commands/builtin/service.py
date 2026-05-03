"""`/service <subcommand> [args...]` — predefined ops against ServiceSpec hosts.

Subcommand registration is **dynamic**: at install() time we scan
config.services for every action name across every service and
auto-register a `/service <action>` subcommand for each. Adding a
new action = a one-line yaml edit + a bot restart, no code change.

Two metadata subcommands stay hardcoded because they aren't actions:

  /service list           (SAFE) — list configured services
  /service info <name>    (SAFE) — show one service's hosts + actions

Each dynamically-registered action subcommand inherits its Risk +
fan-out behavior from the action's *name*:

  - Names in _SAFE_ACTION_NAMES (status / logs / sysinfo / df ...)
    are SAFE and fan out across every host in parallel.
  - Everything else is PRIVILEGED, requires TOTP, and refuses to
    run without an explicit `--host X`. Conservative default:
    "I don't know what `deploy` does, ask twice."

ACL key + audit `command=` field stay as `service_<action>` so
existing config and historical records keep working.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from bot_cmder.connectors.base import ExecResult
from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import OutgoingResponse
from bot_cmder.core.registry import CommandRegistry, Risk, Router

if TYPE_CHECKING:
    from bot_cmder.audit.log import AuditLogger
    from bot_cmder.config.schema import AppConfig, ServiceSpec
    from bot_cmder.connectors.ssh import SshConnectorPool

logger = logging.getLogger(__name__)


# Action names whose name suggests "read-only / cheap diagnostic" and
# therefore default to Risk.SAFE + fan-out across all hosts. Anything
# else is treated as PRIVILEGED + requires --host. Override per-action
# risk would land via a richer ServiceSpec.actions schema later
# (current schema is just `dict[str, str]`).
_SAFE_ACTION_NAMES: frozenset[str] = frozenset(
    {
        "status",
        "info",
        "logs",
        "log",
        "health",
        "check",
        "show",
        "inspect",
        "sysinfo",
        "uname",
        "uptime",
        "df",
        "diskfree",
        "diskusage",
        "free",
        "top",
        "ps",
        "ls",
        "list",
        "cat",
        "tail",
        "head",
        "ping",
        "tracepath",
    }
)

# Names that the metadata subcommands already own. A user-defined
# action with one of these names would silently shadow the metadata
# command; we log a warning and skip registration instead.
_RESERVED_SUBCOMMANDS: frozenset[str] = frozenset({"list", "info", "help"})


def _classify(action_name: str) -> Risk:
    return Risk.SAFE if action_name in _SAFE_ACTION_NAMES else Risk.PRIVILEGED


def install(
    registry: CommandRegistry,
    *,
    ssh_pool: SshConnectorPool,
    audit: AuditLogger,
    config: AppConfig,
) -> None:
    router = registry.create_router(
        "service",
        description="Run predefined ops actions against configured services",
    )

    @router.subcommand(
        "list",
        risk=Risk.SAFE,
        description="List configured services and their hosts",
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

    @router.subcommand(
        "info",
        risk=Risk.SAFE,
        description="Show one service in detail (hosts + action commands)",
    )
    async def _info(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
        if len(args) != 1:
            return OutgoingResponse.text_reply("usage: /service info <name>")
        name = args[0]
        spec = ctx.config.services.get(name)
        if spec is None:
            known = ", ".join(sorted(ctx.config.services)) or "<none>"
            return OutgoingResponse.text_reply(f"unknown service: {name} (known: {known})")
        lines = [f"Service: {name}"]
        lines.append(f"  Hosts: {', '.join(spec.hosts) or '<none>'}")
        if spec.actions:
            lines.append("  Actions:")
            name_w = max(len(a) for a in spec.actions)
            for action_name, command in sorted(spec.actions.items()):
                risk = _classify(action_name).value
                lines.append(f"    {action_name.ljust(name_w)}  [{risk}]  {command}")
        else:
            lines.append("  Actions: <none>")
        return OutgoingResponse.text_reply("\n".join(lines))

    # Dynamic action subcommands. Collect the union of action names
    # across every configured service so that a new yaml entry alone
    # is enough to surface the corresponding /service <action>.
    action_names: set[str] = set()
    for spec in config.services.values():
        action_names.update(spec.actions.keys())

    for action_name in sorted(action_names):
        if action_name in _RESERVED_SUBCOMMANDS:
            logger.warning(
                "service action %r shadows the reserved /service %s subcommand; skipping registration. "
                "Rename the action in config.services to expose it on chat.",
                action_name,
                action_name,
            )
            continue
        _register_action_subcommand(
            router=router,
            action_name=action_name,
            risk=_classify(action_name),
            ssh_pool=ssh_pool,
            audit=audit,
        )


def _register_action_subcommand(
    *,
    router: Router,
    action_name: str,
    risk: Risk,
    ssh_pool: SshConnectorPool,
    audit: AuditLogger,
) -> None:
    if risk == Risk.SAFE:
        description = f"Run the `{action_name}` action across every host in parallel"
    else:
        description = f"Run the `{action_name}` action on ONE host (--host X)"

    @router.subcommand(action_name, risk=risk, description=description, timeout_s=60)
    async def _handler(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
        if risk == Risk.SAFE:
            if len(args) != 1:
                return OutgoingResponse.text_reply(f"usage: /service {action_name} <name>")
            return await _run_fan_out(ctx, args[0], action=action_name, ssh_pool=ssh_pool, audit=audit)
        return await _run_single_host(ctx, args, action=action_name, ssh_pool=ssh_pool, audit=audit)


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
        platform=ctx.platform.value,
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
        return OutgoingResponse.text_reply(f"usage: /service {action} <name> --host <host>")
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
        platform=ctx.platform.value,
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
    """Render a fan-out result: status table + per-host stdout/stderr.

    The status line alone is right for `/service status` (where the
    answer IS pass/fail), but for read actions like `/service sysinfo`
    or `/service df` the WHOLE point is the output. Always show stdout
    (and stderr on non-zero exit), trimmed per host so 5 hosts ×
    `journalctl -n 100` doesn't blow Telegram's 4096-char message cap.
    """
    name_w = max((len(h) for h, _ in results), default=4)
    lines = [f"{service_name} {action}:"]
    for host, r in results:
        lines.append(f"  {host.ljust(name_w)}  {_short_status(r)}")

    for host, r in results:
        if isinstance(r, str):
            # Pre-exec failure (e.g. unknown host); short_status above
            # already explained it, no extra body to print.
            continue
        out_block = _trim_for_fanout(r.stdout)
        err_block = _trim_for_fanout(r.stderr) if r.exit_code != 0 else ""
        if not out_block and not err_block:
            continue
        lines.append("")
        lines.append(f"--- {host} ---")
        if out_block:
            lines.append(out_block)
        if err_block:
            if out_block:
                lines.append("")
            lines.append(f"(stderr) {err_block}")
    return "\n".join(lines)


def _trim_for_fanout(text: str, *, max_lines: int = 8, max_chars: int = 600) -> str:
    """Cap a per-host stdout/stderr block so the combined fan-out reply
    stays under Telegram's 4096-char message limit even with several
    hosts. Returns empty string when the input has no real content."""
    text = text.rstrip()
    if not text:
        return ""
    src_lines = text.splitlines()
    visible = src_lines[:max_lines]
    out = "\n".join(visible)
    truncated_chars = False
    if len(out) > max_chars:
        out = out[:max_chars]
        truncated_chars = True
    if truncated_chars or len(src_lines) > max_lines:
        out += "\n[...truncated]"
    return out


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
