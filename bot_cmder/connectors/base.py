from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    target: str
    truncated: bool = False


class Connector(ABC):
    """How the bot talks to an execution target.

    A Connector instance is bound to its target (e.g. local subprocess
    or one specific SSH host) at construction time. The execute() call
    runs an argv-style command against that target with a hard timeout.

    Subclasses must NEVER use shell=True or string interpolation into
    a shell — argv is passed list-form to the underlying executor.
    """

    target: str  # human-readable label, e.g. "local" or "ssh:server-a"

    @abstractmethod
    async def execute(
        self,
        argv: list[str],
        *,
        timeout_s: int = 30,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        max_output_bytes: int = 3500,
    ) -> ExecResult: ...
