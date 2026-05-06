"""bot-cmder CLI dispatcher.

Wires up every subcommand module's `add_subparsers()` against one
top-level argparse parser, then routes to the handler each subcommand
stashed via `set_defaults(func=...)`. Reachable as both:

    bot-cmder <subcommand> [args...]      # via [project.scripts] in pyproject.toml
    python -m bot_cmder <subcommand> ...  # via bot_cmder/__main__.py

Tests import `main` directly:

    from bot_cmder.cli import main
    main(["enroll-totp", "--user", "telegram:111"])

Adding a new subcommand: drop a module under `bot_cmder/cli/`, expose
`add_subparsers(sub)`, add the import + call below. Keep imports lazy
inside subcommand handlers (see serve.py, keys.py) so `bot-cmder
--help` stays snappy.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from bot_cmder import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bot-cmder",
        description="Multi-platform SRE ChatOps bot — Telegram / Discord / Slack.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"bot-cmder {__version__}",
    )
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="<subcommand>")

    # Each module owns its own subcommand registration so this
    # dispatcher stays a thin wiring file. Order here only affects the
    # `--help` listing — runtime routing goes through `args.func`.
    from bot_cmder.cli import discord_register, init_cmd, keys, serve, slack_manifest, totp

    serve.add_subparsers(sub)
    init_cmd.add_subparsers(sub)
    keys.add_subparsers(sub)
    totp.add_subparsers(sub)
    discord_register.add_subparsers(sub)
    slack_manifest.add_subparsers(sub)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
