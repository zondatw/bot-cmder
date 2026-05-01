from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import OutgoingResponse


class Risk(str, Enum):
    SAFE = "safe"
    PRIVILEGED = "privileged"


CommandHandler = Callable[[CommandContext, list[str]], Awaitable[OutgoingResponse]]


@dataclass
class Command:
    name: str
    risk: Risk
    handler: CommandHandler
    description: str = ""
    allowed: list[str] | None = None
    requires_2fa: bool | None = None
    timeout_s: int = 30

    @property
    def effective_2fa(self) -> bool:
        if self.requires_2fa is not None:
            return self.requires_2fa
        return self.risk == Risk.PRIVILEGED


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(self, cmd: Command) -> None:
        if cmd.name in self._commands:
            raise ValueError(f"command {cmd.name!r} already registered")
        self._commands[cmd.name] = cmd

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def all(self) -> list[Command]:
        return list(self._commands.values())

    def clear(self) -> None:
        """For tests."""
        self._commands.clear()


REGISTRY = CommandRegistry()


def register(
    name: str,
    risk: Risk = Risk.SAFE,
    *,
    description: str = "",
    allowed: list[str] | None = None,
    requires_2fa: bool | None = None,
    timeout_s: int = 30,
    registry: CommandRegistry | None = None,
) -> Callable[[CommandHandler], CommandHandler]:
    def deco(fn: CommandHandler) -> CommandHandler:
        target = registry or REGISTRY
        target.register(
            Command(
                name=name,
                risk=risk,
                handler=fn,
                description=description,
                allowed=allowed,
                requires_2fa=requires_2fa,
                timeout_s=timeout_s,
            )
        )
        return fn

    return deco
