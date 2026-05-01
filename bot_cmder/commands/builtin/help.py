from __future__ import annotations

from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import OutgoingResponse
from bot_cmder.core.registry import CommandRegistry, Risk, register


def install(registry: CommandRegistry) -> None:
    @register("help", risk=Risk.SAFE, description="Show available commands", registry=registry)
    async def _help(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
        # Merge top-level Commands and Routers into one alphabetical
        # listing so users don't have to know which is which. Routers
        # show their description; the per-router subcommand drilldown
        # is reachable via `/<router> help` and is documented in the
        # entry's description tail.
        lines = ["Available commands:"]
        entries: list[tuple[str, str, str]] = []  # (name, tag, description)
        for cmd in registry.all():
            tag = " [privileged]" if cmd.risk == Risk.PRIVILEGED else ""
            entries.append((cmd.name, tag, cmd.description or ""))
        for router in registry.all_routers():
            sub_count = len(router.subcommand_names())
            tag = ""  # mixed risks across subcommands
            desc = f"{router.description} (try `/{router.name} help` — {sub_count} subcommands)"
            entries.append((router.name, tag, desc))
        for name, tag, desc in sorted(entries, key=lambda e: e[0]):
            lines.append(f"  /{name}{tag} — {desc}".rstrip(" —"))
        return OutgoingResponse.text_reply("\n".join(lines))
