"""`/kubectl <subcommand> [args...]` — whitelisted kubectl access.

Privileged: gated by TOTP via the dispatcher.

The first arg is matched against `kubectl.allowed_subcommands` from
config (default: get / describe / logs / rollout / scale / top).
Anything outside that set is rejected before the subprocess is spawned.

Execution goes through LocalConnector (asyncio.create_subprocess_exec,
never shell=True), so the remaining args are passed argv-form and
shell metacharacters in user input are inert.
"""

from __future__ import annotations

from bot_cmder.connectors.base import ExecResult
from bot_cmder.connectors.local import LocalConnector
from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import OutgoingResponse
from bot_cmder.core.registry import CommandRegistry, Risk, register


def install(registry: CommandRegistry, *, connector: LocalConnector | None = None) -> None:
    local = connector or LocalConnector()

    @register(
        "kubectl",
        risk=Risk.PRIVILEGED,
        description="Run a whitelisted kubectl subcommand (get/describe/logs/rollout/scale/top)",
        registry=registry,
        timeout_s=30,
    )
    async def _kubectl(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
        cfg = ctx.config.kubectl
        if not args:
            return OutgoingResponse.text_reply(f"usage: /kubectl <{'|'.join(cfg.allowed_subcommands)}> [args...]")
        sub, *rest = args
        if sub not in cfg.allowed_subcommands:
            return OutgoingResponse.text_reply(
                f"subcommand '{sub}' not in allowlist: " + ", ".join(cfg.allowed_subcommands)
            )

        env: dict[str, str] | None = None
        if cfg.kubeconfig is not None:
            env = {"KUBECONFIG": str(cfg.kubeconfig)}

        result = await local.execute(
            ["kubectl", sub, *rest],
            env=env,
            max_output_bytes=cfg.max_output_bytes,
            timeout_s=30,
        )
        return OutgoingResponse.text_reply(_format(result, ["kubectl", sub, *rest]))


def _format(r: ExecResult, argv: list[str]) -> str:
    cmd = " ".join(argv)
    header = f"$ {cmd}\n(exit={r.exit_code}, {r.duration_ms}ms)"
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
