from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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


@dataclass
class Router:
    """A top-level command that dispatches to subcommands by first arg.

    Each subcommand is a fully-fledged Command (its own Risk, ACL key,
    OTP gating) registered under the synthetic name `<router>_<sub>`.
    The dispatcher rewrites `/<router> <sub> <args...>` to look up
    that internal name, so ACL config (`acl.commands.service_restart`)
    and audit (`command="service_restart"`) keep working unchanged.

    Empty args, the literal `help`, or an unknown subcommand prints
    the auto-generated help text from `help_text()`.
    """

    name: str
    description: str = ""
    _subcommands: dict[str, Command] = field(default_factory=dict)

    def subcommand(
        self,
        sub_name: str,
        risk: Risk = Risk.SAFE,
        *,
        description: str = "",
        allowed: list[str] | None = None,
        requires_2fa: bool | None = None,
        timeout_s: int = 30,
    ) -> Callable[[CommandHandler], CommandHandler]:
        """Decorator: register `fn` as the `<router>_<sub_name>` handler."""

        def deco(fn: CommandHandler) -> CommandHandler:
            internal_name = f"{self.name}_{sub_name}"
            if sub_name in self._subcommands:
                raise ValueError(f"subcommand {sub_name!r} already registered on router {self.name!r}")
            self._subcommands[sub_name] = Command(
                name=internal_name,
                risk=risk,
                handler=fn,
                description=description,
                allowed=allowed,
                requires_2fa=requires_2fa,
                timeout_s=timeout_s,
            )
            return fn

        return deco

    def get_subcommand(self, sub_name: str) -> Command | None:
        return self._subcommands.get(sub_name)

    def subcommand_names(self) -> list[str]:
        return sorted(self._subcommands)

    def all_subcommands(self) -> list[Command]:
        return [self._subcommands[k] for k in self.subcommand_names()]

    def help_text(self) -> str:
        if not self._subcommands:
            return f"/{self.name} — {self.description} (no subcommands registered)"
        lines = [f"/{self.name} — {self.description}", "", "Subcommands:"]
        name_w = max(len(s) for s in self.subcommand_names())
        for sub_name in self.subcommand_names():
            cmd = self._subcommands[sub_name]
            tag = " [privileged]" if cmd.risk == Risk.PRIVILEGED else ""
            desc = cmd.description or ""
            lines.append(f"  /{self.name} {sub_name.ljust(name_w)}{tag} — {desc}".rstrip(" —"))
        return "\n".join(lines)


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._routers: dict[str, Router] = {}

    # --- top-level Commands ------------------------------------------------

    def register(self, cmd: Command) -> None:
        if cmd.name in self._commands:
            raise ValueError(f"command {cmd.name!r} already registered")
        if cmd.name in self._routers:
            raise ValueError(f"name {cmd.name!r} clashes with a router")
        self._commands[cmd.name] = cmd

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def all(self) -> list[Command]:
        """Top-level Commands only — Router subcommands not included.

        Used by /help and the Telegram menu sync, which want one entry
        per top-level chat command (the routers themselves count as one
        entry each, surfaced via `all_routers()`).
        """
        return list(self._commands.values())

    def clear(self) -> None:
        """For tests."""
        self._commands.clear()
        self._routers.clear()

    # --- Routers -----------------------------------------------------------

    def register_router(self, router: Router) -> None:
        if router.name in self._routers:
            raise ValueError(f"router {router.name!r} already registered")
        if router.name in self._commands:
            raise ValueError(f"name {router.name!r} clashes with a command")
        self._routers[router.name] = router

    def create_router(self, name: str, description: str = "") -> Router:
        """Convenience: build + register a fresh Router and return it."""
        router = Router(name=name, description=description)
        self.register_router(router)
        return router

    def get_router(self, name: str) -> Router | None:
        return self._routers.get(name)

    def all_routers(self) -> list[Router]:
        return list(self._routers.values())

    # --- introspection across both ----------------------------------------

    def all_executable_commands(self) -> list[Command]:
        """Every Command users can actually invoke — top-level + every
        router's subcommand. Used for audit / debug, NOT for /help or
        the Telegram menu (those want top-level only)."""
        out = list(self._commands.values())
        for r in self._routers.values():
            out.extend(r.all_subcommands())
        return out


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
