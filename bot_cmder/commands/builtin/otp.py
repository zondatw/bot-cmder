"""`/otp <code>` — submit a TOTP for the user's pending privileged command.

Pops the PendingOTPSession (if any), enforces same-chat / same-platform
delivery, validates the code via TOTPVerifier, then re-runs the
original command's handler with its stashed args.

Distinct audit events for each failure mode so post-mortems can tell
"no session" from "wrong code" from "leaked across chats".
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import OutgoingResponse
from bot_cmder.core.registry import CommandRegistry, Risk, register

if TYPE_CHECKING:
    from bot_cmder.audit.log import AuditLogger
    from bot_cmder.auth.pending import PendingOTPSessions
    from bot_cmder.auth.totp import TOTPVerifier


def install(
    registry: CommandRegistry,
    *,
    pending: PendingOTPSessions,
    totp: TOTPVerifier,
    audit: AuditLogger,
) -> None:
    @register(
        "otp",
        risk=Risk.SAFE,
        description="Submit OTP code to authorize a pending privileged command",
        registry=registry,
    )
    async def _otp(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
        if len(args) != 1:
            return OutgoingResponse.text_reply("usage: /otp <6-digit-code>")
        code = args[0]

        session = pending.pop(ctx.user.norm_id)
        if session is None:
            audit.log(
                event="OTP_NO_PENDING",
                user=ctx.user.norm_id,
                chat=ctx.chat_id,
            )
            return OutgoingResponse.text_reply("no pending privileged command (or it expired)")

        if session.chat_id != ctx.chat_id or session.platform != ctx.platform:
            audit.log(
                event="OTP_CROSS_CHAT",
                user=ctx.user.norm_id,
                chat=ctx.chat_id,
                command=session.command_name,
                pending_chat=session.chat_id,
                pending_platform=session.platform.value,
            )
            return OutgoingResponse.text_reply("OTP must be submitted in the same chat as the original command")

        if pending.is_expired(session):
            audit.log(
                event="OTP_EXPIRED",
                user=ctx.user.norm_id,
                chat=ctx.chat_id,
                command=session.command_name,
            )
            return OutgoingResponse.text_reply("session expired; rerun the original command")

        if not totp.verify(ctx.user.norm_id, code):
            audit.log(
                event="OTP_INVALID",
                user=ctx.user.norm_id,
                chat=ctx.chat_id,
                command=session.command_name,
            )
            return OutgoingResponse.text_reply("invalid code")

        # Resolve and run the original command.
        cmd = registry.get(session.command_name)
        if cmd is None:
            audit.log(
                event="OTP_COMMAND_GONE",
                user=ctx.user.norm_id,
                chat=ctx.chat_id,
                command=session.command_name,
            )
            return OutgoingResponse.text_reply(f"command no longer registered: /{session.command_name}")

        try:
            response = await asyncio.wait_for(cmd.handler(ctx, session.args), timeout=cmd.timeout_s)
        except asyncio.TimeoutError:
            audit.log(
                event="COMMAND_TIMEOUT",
                user=ctx.user.norm_id,
                chat=ctx.chat_id,
                command=cmd.name,
                args=session.args,
                via_otp=True,
                timeout_s=cmd.timeout_s,
            )
            return OutgoingResponse.text_reply(f"timeout after {cmd.timeout_s}s")
        except Exception as exc:
            audit.log(
                event="HANDLER_ERROR",
                user=ctx.user.norm_id,
                chat=ctx.chat_id,
                command=cmd.name,
                args=session.args,
                via_otp=True,
                error=repr(exc),
            )
            return OutgoingResponse.text_reply(f"error: {exc}")

        audit.log(
            event="EXECUTED",
            user=ctx.user.norm_id,
            chat=ctx.chat_id,
            command=cmd.name,
            args=session.args,
            via_otp=True,
        )
        return response
