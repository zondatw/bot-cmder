"""`bot-cmder serve` — start the FastAPI bot via uvicorn.

Replaces the deleted `server.py` script. Same env-var contract for
host/port/reload, but with `BOT_CMDER_*` namespaced names canonical
and the legacy `BIND_HOST` / `BIND_PORT` / `RELOAD` honored as
deprecated aliases for one minor release (issue #20).

Flag precedence (highest wins):
    --host / --port / --reload  CLI flags
    BOT_CMDER_HOST / BOT_CMDER_PORT / BOT_CMDER_RELOAD env vars
    BIND_HOST / BIND_PORT / RELOAD  (DEPRECATED, with warning)
    hardcoded defaults (127.0.0.1 / 47823 / false)

The default host is loopback-only on purpose — exposing the bot on
all interfaces should be an explicit choice (`--host 0.0.0.0` or
the env var). The 47823 port matches what the deleted server.py
used to default to, so existing reverse-proxy / tunnel configs
keep working without any operator action.
"""

from __future__ import annotations

import argparse
import logging
import os
import warnings

logger = logging.getLogger(__name__)

# Track which deprecated env vars we've already warned about, so the
# warning surfaces once per process instead of N-times-per-call. The
# CLI runs once and exits, but if `cmd_serve` is ever called from
# tests in a loop, this still keeps output sane.
_warned: set[str] = set()


def _env_or_alias(new: str, old: str) -> str | None:
    """Read an env var with a deprecated-alias fallback.

    Returns the value, or None if neither name is set. When the alias
    fires, emits both a `DeprecationWarning` (callers can capture in
    pytest, suppress, etc.) and a logger.warning (visible in the
    runtime log of the SRE who runs the bot).
    """
    val = os.environ.get(new)
    if val is not None:
        return val
    val = os.environ.get(old)
    if val is not None and old not in _warned:
        _warned.add(old)
        warnings.warn(
            f"{old} is deprecated; use {new}. " "Both names are honored in 0.2.x; 0.3.0 will drop the legacy form.",
            DeprecationWarning,
            stacklevel=2,
        )
        logger.warning("%s is deprecated; use %s (will be removed in 0.3.0)", old, new)
    return val


def _coerce_bool(s: str | None, default: bool) -> bool:
    """Parse `1` / `true` / `yes` / `on` (case-insensitive) as truthy."""
    if s is None:
        return default
    return s.strip().lower() in {"1", "true", "yes", "on"}


def _coerce_int(s: str | None, default: int) -> int:
    if s is None:
        return default
    try:
        return int(s)
    except ValueError:
        logger.warning("ignoring non-integer port value %r; falling back to default %d", s, default)
        return default


def cmd_serve(args: argparse.Namespace) -> int:
    """Launch uvicorn against `bot_cmder.main:app`.

    Imported lazily so `bot-cmder --help` and unrelated subcommands
    don't pay the FastAPI / pydantic-settings import cost.
    """
    import uvicorn

    host = args.host or _env_or_alias("BOT_CMDER_HOST", "BIND_HOST") or "127.0.0.1"
    port = args.port if args.port is not None else _coerce_int(_env_or_alias("BOT_CMDER_PORT", "BIND_PORT"), 47823)
    reload_flag = args.reload or _coerce_bool(_env_or_alias("BOT_CMDER_RELOAD", "RELOAD"), False)

    logger.info("bot-cmder serve: host=%s port=%d reload=%s", host, port, reload_flag)
    uvicorn.run(
        "bot_cmder.main:app",
        host=host,
        port=port,
        reload=reload_flag,
        factory=False,
    )
    return 0


def add_subparsers(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "serve",
        help="Start the bot (uvicorn-hosted FastAPI app)",
        description=(
            "Start the bot. Reads config from the search order described in "
            "bot_cmder.config.paths (CWD-first, XDG-fallback). Set "
            "BOT_CMDER_HOST / BOT_CMDER_PORT / BOT_CMDER_RELOAD to override "
            "the defaults from .env or the shell."
        ),
    )
    p.add_argument("--host", default=None, help="Bind address (default: BOT_CMDER_HOST or 127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="Bind port (default: BOT_CMDER_PORT or 47823)")
    p.add_argument(
        "--reload",
        action="store_true",
        help="Enable uvicorn auto-reload (dev only). Equivalent to BOT_CMDER_RELOAD=1.",
    )
    p.set_defaults(func=cmd_serve)
