"""`/runbook-list` and `/runbook-run <name> [args...]`.

A runbook is any executable file in `runbook.dir` (config, default
./runbooks). The "name" is the filename without its extension —
`restart-api.sh` shows up as `restart-api`. Treat the directory as
code: gitops it, code-review additions; whoever can write to it
controls everything bot-cmder can do.

Two distinct registered commands so the safe `list` action and the
privileged `run` action have separate Risk levels.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from bot_cmder.connectors.base import ExecResult
from bot_cmder.connectors.local import LocalConnector
from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import OutgoingResponse
from bot_cmder.core.registry import CommandRegistry, Risk, register

# A runbook name has to be a single safe filename stem — no slashes
# (path traversal), no leading dots (hidden files), nothing weird.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# Args passed to the runbook process. We pass them argv-form so shell
# metacharacters are inert, but we still narrow the alphabet so a
# typo'd `; rm -rf /` argument can't be passed at all (defense in
# depth against scripts that themselves shell out).
_ARG_RE = re.compile(r"^[\w./=:+@-]+$")


def _list_runbooks(directory: Path) -> list[Path]:
    if not directory.exists() or not directory.is_dir():
        return []
    out = []
    for entry in sorted(directory.iterdir()):
        if entry.is_file() and not entry.name.startswith(".") and os.access(entry, os.X_OK):
            out.append(entry)
    return out


def _find_runbook(directory: Path, name: str) -> Path | None:
    if not _NAME_RE.match(name):
        return None
    for entry in _list_runbooks(directory):
        if entry.stem == name:
            return entry
    return None


def install(registry: CommandRegistry, *, connector: LocalConnector | None = None) -> None:
    local = connector or LocalConnector()

    @register(
        "runbook-list",
        risk=Risk.SAFE,
        description="List available runbooks",
        registry=registry,
    )
    async def _list(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
        scripts = _list_runbooks(Path(ctx.config.runbook.dir))
        if not scripts:
            return OutgoingResponse.text_reply(f"no runbooks in {ctx.config.runbook.dir}")
        lines = [f"Runbooks in {ctx.config.runbook.dir}:"]
        for s in scripts:
            lines.append(f"  {s.stem.ljust(20)} ({s.name})")
        return OutgoingResponse.text_reply("\n".join(lines))

    @register(
        "runbook-run",
        risk=Risk.PRIVILEGED,
        description="Run a runbook by name with optional args",
        registry=registry,
        timeout_s=120,
    )
    async def _run(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
        if not args:
            return OutgoingResponse.text_reply("usage: /runbook-run <name> [args...]")
        name, *rest = args
        path = _find_runbook(Path(ctx.config.runbook.dir), name)
        if path is None:
            return OutgoingResponse.text_reply(f"unknown runbook: {name} (try /runbook-list)")
        bad = [a for a in rest if not _ARG_RE.match(a)]
        if bad:
            return OutgoingResponse.text_reply(
                "refused: runbook args may only contain [A-Za-z0-9_./=:+@-], " f"got disallowed: {bad}"
            )
        result = await local.execute(
            [str(path), *rest],
            cwd=str(ctx.config.runbook.dir),
            max_output_bytes=ctx.config.runbook.max_output_bytes,
            timeout_s=120,
        )
        return OutgoingResponse.text_reply(_format(result, [path.name, *rest]))


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
