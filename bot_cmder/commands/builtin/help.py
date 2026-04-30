from __future__ import annotations

from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import OutgoingResponse
from bot_cmder.core.registry import CommandRegistry, Risk, register


def install(registry: CommandRegistry) -> None:
    @register("help", risk=Risk.SAFE, description="Show available commands", registry=registry)
    async def _help(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
        commands = sorted(registry.all(), key=lambda c: c.name)
        lines = ["Available commands:"]
        for cmd in commands:
            tag = " [privileged]" if cmd.risk == Risk.PRIVILEGED else ""
            desc = cmd.description or ""
            lines.append(f"  /{cmd.name}{tag} — {desc}".rstrip(" —"))
        return OutgoingResponse.text_reply("\n".join(lines))
