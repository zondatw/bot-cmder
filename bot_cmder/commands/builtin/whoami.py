from __future__ import annotations

from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import OutgoingResponse
from bot_cmder.core.registry import CommandRegistry, Risk, register


def install(registry: CommandRegistry) -> None:
    @register(
        "whoami",
        risk=Risk.SAFE,
        description="Show your normalized identity and role",
        registry=registry,
    )
    async def _whoami(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
        user_cfg = ctx.config.alias_to_user().get(ctx.user.norm_id)
        lines = [
            f"norm_id: {ctx.user.norm_id}",
            f"handle: {ctx.user.handle or '-'}",
            f"display_name: {ctx.user.display_name or '-'}",
        ]
        if user_cfg is not None:
            lines.append(f"id: {user_cfg.id}")
            lines.append(f"role: {user_cfg.role}")
        else:
            lines.append("id: <not in config>")
            lines.append("role: <none>")
        return OutgoingResponse.text_reply("\n".join(lines))
