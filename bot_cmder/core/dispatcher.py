from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import IncomingMessage, OutgoingResponse, ResponseKind
from bot_cmder.core.parser import parse
from bot_cmder.core.registry import Command, CommandRegistry

if TYPE_CHECKING:
    from bot_cmder.audit.log import AuditLogger
    from bot_cmder.config.schema import AppConfig


AclCheck = Callable[[str, Command, "AppConfig"], bool]


@dataclass
class Dispatcher:
    registry: CommandRegistry
    config: AppConfig
    audit: AuditLogger
    acl_check: AclCheck
    now: Callable[[], datetime] = datetime.now

    async def dispatch(self, msg: IncomingMessage) -> OutgoingResponse | None:
        parsed = parse(msg.text)
        if parsed is None:
            return None  # not a command, ignore silently

        cmd = self.registry.get(parsed.name)
        if cmd is None:
            self.audit.log(
                event="COMMAND_UNKNOWN",
                user=msg.user.norm_id,
                chat=msg.chat_id,
                command=parsed.name,
                args=parsed.args,
            )
            return OutgoingResponse.text_reply(f"unknown command: /{parsed.name}")

        if not self.acl_check(msg.user.norm_id, cmd, self.config):
            self.audit.log(
                event="AUTH_DENIED",
                user=msg.user.norm_id,
                chat=msg.chat_id,
                command=cmd.name,
                args=parsed.args,
            )
            return OutgoingResponse.text_reply("forbidden")

        # Phase 2 will insert OTP gate here for cmd.effective_2fa.
        ctx = CommandContext.from_message(msg, self.config, self.now)

        try:
            response = await asyncio.wait_for(cmd.handler(ctx, parsed.args), timeout=cmd.timeout_s)
        except asyncio.TimeoutError:
            self.audit.log(
                event="COMMAND_TIMEOUT",
                user=msg.user.norm_id,
                chat=msg.chat_id,
                command=cmd.name,
                args=parsed.args,
                timeout_s=cmd.timeout_s,
            )
            return OutgoingResponse.text_reply(f"timeout after {cmd.timeout_s}s")
        except Exception as exc:
            self.audit.log(
                event="HANDLER_ERROR",
                user=msg.user.norm_id,
                chat=msg.chat_id,
                command=cmd.name,
                args=parsed.args,
                error=repr(exc),
            )
            return OutgoingResponse(kind=ResponseKind.TEXT, text=f"error: {exc}")

        self.audit.log(
            event="EXECUTED",
            user=msg.user.norm_id,
            chat=msg.chat_id,
            command=cmd.name,
            args=parsed.args,
        )
        return response
