from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress

from bot_cmder.connectors.base import Connector, ExecResult


class LocalConnector(Connector):
    """Runs commands as a subprocess on the bot host."""

    target = "local"

    async def execute(
        self,
        argv: list[str],
        *,
        timeout_s: int = 30,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        max_output_bytes: int = 3500,
    ) -> ExecResult:
        if not argv:
            raise ValueError("argv must not be empty")

        merged_env = {**os.environ, **(env or {})}
        started = time.monotonic()

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            cwd=cwd,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            with suppress(ProcessLookupError):
                await proc.wait()
            duration_ms = int((time.monotonic() - started) * 1000)
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=f"timeout after {timeout_s}s",
                duration_ms=duration_ms,
                target=self.target,
                truncated=False,
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout, stdout_truncated = _truncate(stdout_b, max_output_bytes)
        stderr, stderr_truncated = _truncate(stderr_b, max_output_bytes)

        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            target=self.target,
            truncated=stdout_truncated or stderr_truncated,
        )


def _truncate(data: bytes, limit: int) -> tuple[str, bool]:
    if len(data) <= limit:
        return data.decode("utf-8", errors="replace"), False
    head = data[:limit].decode("utf-8", errors="replace")
    return head + f"\n[...truncated {len(data) - limit} bytes]", True
