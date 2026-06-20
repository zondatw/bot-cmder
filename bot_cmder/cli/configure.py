"""`bot-cmder configure` — interactive credential wizard (issue #45).

Walks operators through populating their `.env` with Telegram /
Discord / Slack credentials after `bot-cmder init` has scaffolded
the file with placeholders.

This module is the skeleton: TTY detection, the menu-mode select
widget, dispatcher wiring, dry-run / non-interactive plumbing. The
per-platform flow functions (`_flow_telegram` / `_flow_discord` /
`_flow_slack`) are stubs landed here that simply note the platform
is "not yet wired" — C4 / C5 / C6 of issue #45 fill those in with
the real walkthroughs.

Lazy-imports `questionary` inside `cmd_configure` so unrelated
subcommands (`serve`, `init`, `--help`) don't pay the
prompt_toolkit import cost. See pyproject.toml's note on
"questionary >= 2.0".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bot_cmder.cli._envfile import EnvFile
from bot_cmder.config.paths import config_dir

# --- shared helpers ----------------------------------------------------

_PLATFORM_CHOICES = ("telegram", "discord", "slack", "all")

# Maps each platform to the env keys that, if present, indicate the
# platform is "configured". `_platform_status()` walks this map to
# render the menu's status column.
_PLATFORM_KEYS: dict[str, list[str]] = {
    "telegram": ["TELEGRAM_TOKEN"],
    "discord": ["DISCORD_BOT_TOKEN", "DISCORD_APPLICATION_ID"],
    "slack": ["SLACK_BOT_TOKEN"],
}


def _resolve_config_dir(args: argparse.Namespace) -> Path:
    """Same shape as init_cmd._resolve_config_dir — pick the explicit
    --config-dir if given, else fall through to the XDG search.
    Duplicated rather than imported because (a) it's 4 lines and (b)
    cross-module imports between CLI subcommands invite circular-dep
    surprises down the road. DRY-ing is a follow-up if a third
    subcommand grows the same flag."""
    if args.config_dir is not None:
        return Path(args.config_dir).resolve()
    return config_dir()


def _load_env(env_path: Path) -> EnvFile | None:
    """Load `.env` for editing. Returns None (after printing a fix-it
    message to stderr) when:

      - the file doesn't exist (operator hasn't run `bot-cmder init`)
      - `BOT_CMDER_MASTER_KEY` isn't set (file exists but init never
        finished — bot would fail to start anyway)

    Caller propagates the None → exit 1. We don't create the file
    ourselves because that would silently generate a different master
    key than `init` would have, and there's no good way for the
    operator to know `init` was needed when their next `bot-cmder
    serve` fails with "TOTP disabled, BOT_CMDER_MASTER_KEY not set".
    """
    if not env_path.is_file():
        print(
            f"bot-cmder configure: no .env at {env_path}\n" f"  Run `bot-cmder init` first to scaffold it.",
            file=sys.stderr,
        )
        return None
    env = EnvFile.load(env_path)
    if not env.get("BOT_CMDER_MASTER_KEY"):
        print(
            f"bot-cmder configure: BOT_CMDER_MASTER_KEY missing from {env_path}\n"
            f"  Run `bot-cmder init` (or `bot-cmder gen-master-key` and paste the value).",
            file=sys.stderr,
        )
        return None
    return env


def _platform_status(env: EnvFile, platform: str) -> str:
    """One-line summary for the menu. Returns 'configured', 'partial:
    missing X', or 'unset' based on which of the platform's required
    env keys are present."""
    keys = _PLATFORM_KEYS[platform]
    present = [k for k in keys if env.get(k)]
    missing = [k for k in keys if not env.get(k)]
    if not present:
        return "unset"
    if missing:
        return f"partial: missing {', '.join(missing)}"
    return "configured"


def _can_prompt(args: argparse.Namespace, *, reason: str) -> bool:
    """True if a prompt is allowed to fire — neither --non-interactive
    was passed nor is stdin a non-TTY. On False, prints the
    operator-facing hint about what was about to be asked, so the
    caller just needs to `return 1`."""
    if args.non_interactive:
        print(
            f"bot-cmder configure: --non-interactive set, would have prompted: {reason}",
            file=sys.stderr,
        )
        return False
    if not sys.stdin.isatty():
        print(
            f"bot-cmder configure: stdin is not a TTY, would have prompted: {reason}\n"
            f"  Pass --non-interactive in CI to make this failure mode explicit.",
            file=sys.stderr,
        )
        return False
    return True


# --- platform flow stubs (C4 / C5 / C6 of issue #45 fill these in) -----


def _flow_telegram(env: EnvFile, args: argparse.Namespace) -> bool:
    """Walk Telegram credential setup. Returns True if env was modified.
    Placeholder — real walkthrough lands in C4 of issue #45."""
    print("  Telegram flow not yet wired (lands in C4 of issue #45)", file=sys.stderr)
    return False


def _flow_discord(env: EnvFile, args: argparse.Namespace) -> bool:
    """Walk Discord credential setup. Returns True if env was modified.
    Placeholder — real walkthrough lands in C5 of issue #45."""
    print("  Discord flow not yet wired (lands in C5 of issue #45)", file=sys.stderr)
    return False


def _flow_slack(env: EnvFile, args: argparse.Namespace) -> bool:
    """Walk Slack credential setup. Returns True if env was modified.
    Placeholder — real walkthrough lands in C6 of issue #45."""
    print("  Slack flow not yet wired (lands in C6 of issue #45)", file=sys.stderr)
    return False


_FLOWS = {
    "telegram": _flow_telegram,
    "discord": _flow_discord,
    "slack": _flow_slack,
}


# --- menu --------------------------------------------------------------


def _show_menu(env_path: Path, env: EnvFile) -> str | None:
    """Render the top-level platform-picker. Returns the chosen
    platform name, 'all', or None if the operator picked Quit.

    Caller must check `_can_prompt(...)` before calling — this helper
    assumes interactivity and just delegates to questionary.

    Lazy-imports questionary so this module's import cost stays
    near-zero for unrelated subcommands.
    """
    import questionary

    choices = []
    for platform in ("telegram", "discord", "slack"):
        status = _platform_status(env, platform)
        choices.append(
            questionary.Choice(
                title=f"{platform.title():<10} [{status}]",
                value=platform,
            )
        )
    choices.append(questionary.Separator())
    choices.append(
        questionary.Choice(
            title="All        walk Telegram → Discord → Slack",
            value="all",
        )
    )
    choices.append(questionary.Choice(title="Quit", value=None))

    return questionary.select(
        message=f"Configure adapter credentials for {env_path}",
        choices=choices,
        use_shortcuts=False,
    ).unsafe_ask()


# --- entry handler -----------------------------------------------------


def cmd_configure(args: argparse.Namespace) -> int:
    """Wizard entry point. Reads .env, runs the chosen platform flow(s),
    optionally previews / writes the result.

    Returns 0 on success (including "user picked Quit, no changes"),
    1 on operator-facing errors (missing .env, missing master key,
    non-interactive triggered), 2 on file-write failures, 130 on
    Ctrl-C.
    """
    cfg_dir = _resolve_config_dir(args)
    env_path = cfg_dir / ".env"
    env = _load_env(env_path)
    if env is None:
        return 1
    original = EnvFile.load(env_path)  # snapshot for diff

    try:
        # Choose what to walk: explicit positional, or menu pick
        platform: str | None
        if args.platform is None:
            if not _can_prompt(args, reason="top-level platform picker"):
                return 1
            platform = _show_menu(env_path, env)
            if platform is None:
                print("Nothing to do — picked Quit.")
                return 0
        else:
            platform = args.platform

        # Dispatch
        any_changed = False
        flows_to_run = ("telegram", "discord", "slack") if platform == "all" else (platform,)
        for p in flows_to_run:
            if _FLOWS[p](env, args):
                any_changed = True
    except KeyboardInterrupt:
        print("\nAborted, no changes written.", file=sys.stderr)
        return 130

    if not any_changed:
        print("No changes to write.")
        return 0

    # Preview / write
    diff = env.diff(original)
    if args.dry_run:
        print(diff or "(no textual diff — internal state mutation only)")
        return 0

    try:
        env.save()
    except OSError as exc:
        print(f"bot-cmder configure: failed to write {env_path}: {exc}", file=sys.stderr)
        return 2

    print(f"Wrote {env_path}")
    return 0


def add_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register `configure` on the parent dispatcher's subparser group."""
    p = sub.add_parser(
        "configure",
        help="Interactively populate .env with Telegram / Discord / Slack credentials",
        description=(
            "Interactive wizard that walks you through writing Telegram / "
            "Discord / Slack credentials into your .env file. Run after "
            "`bot-cmder init` to fill in real values. Idempotent: re-run "
            "anytime to add a platform, change a mode, or rotate a token."
        ),
    )
    p.add_argument(
        "platform",
        nargs="?",
        choices=_PLATFORM_CHOICES,
        default=None,
        help="Skip the menu and jump straight to one platform. Omit for menu mode.",
    )
    p.add_argument(
        "--config-dir",
        default=None,
        help=("Override .env location. Default: same XDG search as `bot-cmder init` " "($XDG_CONFIG_HOME/bot-cmder/)."),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the unified diff that would be applied; write nothing.",
    )
    p.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail with exit 1 instead of prompting (useful for CI smoke tests).",
    )
    p.set_defaults(func=cmd_configure)
