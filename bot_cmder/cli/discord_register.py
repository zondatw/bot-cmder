"""`bot-cmder discord-register` — push slash command schema to Discord.

Run after editing the registry (adding a new builtin, renaming an
existing one, etc.) so Discord's autocomplete UI matches what the
bot can actually handle. Idempotent — Discord PUT-replaces the
whole command list each call.

Scoping precedence (highest wins):

  1. --guild=<id> CLI flag — explicit override for one-off pushes
  2. --global CLI flag — force global push, ignore env
  3. DISCORD_GUILD_ID env var — your dev-time default
  4. (nothing set) → push globally

Guild push:  visible only in that one server, updates propagate INSTANTLY.
Global push: visible in every server the bot is in + DMs, ~1h propagation.

The env var is read ONLY by this script — the running bot never
touches it (it's a one-shot registration-time concern, not runtime
config). Documented as such in `bot_cmder/config/settings.py`.

Schema is intentionally flat: every top-level Command and every
Router is one slash command. Commands that take user input get a
single STRING option named `args`. `/otp` is the one exception —
it gets four SUB_COMMAND children (`code` / `emergency` / `end` /
`status`) so Discord's autocomplete UI can prompt for the right
field per variant; see issue #18 for why the previous flat-string
schema was unreachable for the no-arg sub-syntaxes. The
DiscordAdapter recombines /<cmd> <args> (or /<cmd> <sub> <args>)
back into the same text-shaped IncomingMessage Telegram produces,
so the dispatcher stays platform-agnostic.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from bot_cmder.adapters.discord.client import DiscordClient
from bot_cmder.audit.log import AuditLogger
from bot_cmder.auth.pending import PendingOTPSessions
from bot_cmder.auth.secret_store import SecretStore
from bot_cmder.auth.totp import TOTPVerifier
from bot_cmder.commands.builtin import install_all
from bot_cmder.config.settings import get_settings, load_app_config
from bot_cmder.connectors.ssh import SshConnectorPool
from bot_cmder.core.registry import CommandRegistry, Router

# Commands that don't take any free-form arguments — no STRING option.
_NO_ARGS_COMMANDS = frozenset({"help", "whoami"})

# Per-command override: name + description + required flag for the
# single STRING option. Anything not listed here defaults to
# `args` (optional). `/otp` is NOT in this map — it gets the full
# sub-command treatment below (issue #18) instead of one flat option.
_OPTION_OVERRIDES: dict[str, dict[str, Any]] = {}

CHAT_INPUT_TYPE = 1
SUB_COMMAND_TYPE = 1  # option-level (collides numerically with CHAT_INPUT_TYPE — Discord re-uses 1 across both axes)
STRING_OPTION_TYPE = 3
INTEGER_OPTION_TYPE = 4

# Issue #18 — `/otp` exposed as four discrete sub-commands so Discord's
# autocomplete renders each variant with its own option schema. Without
# this, the legacy single-STRING `code` (required:true) option made
# `/otp end` and `/otp status` literally unreachable from the Discord
# client (UI refused to submit with an empty required field) and gave
# users no hint that the field accepted anything other than 6 digits.
_OTP_SUB_COMMANDS: list[dict[str, Any]] = [
    {
        "name": "code",
        "description": "Submit a 6-digit TOTP code for a pending privileged command",
        "type": SUB_COMMAND_TYPE,
        "options": [
            {
                "name": "code",
                "description": "6-digit TOTP code",
                "type": STRING_OPTION_TYPE,
                "required": True,
            }
        ],
    },
    {
        "name": "emergency",
        "description": "Open an OTP-bypass window for incident response (issue #15)",
        "type": SUB_COMMAND_TYPE,
        "options": [
            {
                "name": "minutes",
                "description": "Duration in minutes (server caps at totp.emergency_max_minutes)",
                "type": INTEGER_OPTION_TYPE,
                "required": True,
            }
        ],
    },
    {
        "name": "end",
        "description": "Revoke any active emergency-bypass window for the caller",
        "type": SUB_COMMAND_TYPE,
    },
    {
        "name": "status",
        "description": "Show current emergency-bypass window state",
        "type": SUB_COMMAND_TYPE,
    },
]


def _build_command_schema(name: str, description: str) -> dict[str, Any]:
    if name in _NO_ARGS_COMMANDS:
        return {
            "name": name,
            "description": description[:100] or name,
            "type": CHAT_INPUT_TYPE,
        }
    if name == "otp":
        # Special-cased — see _OTP_SUB_COMMANDS docstring (issue #18).
        return {
            "name": name,
            "description": description[:100] or name,
            "type": CHAT_INPUT_TYPE,
            "options": _OTP_SUB_COMMANDS,
        }
    opt = _OPTION_OVERRIDES.get(
        name,
        {"name": "args", "description": "command arguments", "required": False},
    )
    return {
        "name": name,
        "description": description[:100] or name,
        "type": CHAT_INPUT_TYPE,
        "options": [
            {
                "name": opt["name"],
                "description": opt["description"][:100],
                "type": STRING_OPTION_TYPE,
                "required": opt.get("required", False),
            }
        ],
    }


def _build_router_schema(router: Router) -> dict[str, Any]:
    sub_count = len(router.subcommand_names())
    desc = f"{router.description} ({sub_count} subcommands — see /{router.name} help)"
    return {
        "name": router.name,
        "description": desc[:100],
        "type": CHAT_INPUT_TYPE,
        "options": [
            {
                "name": "args",
                "description": "subcommand + args (e.g. 'restart api --host X')"[:100],
                "type": STRING_OPTION_TYPE,
                "required": False,
            }
        ],
    }


def build_manifest(registry: CommandRegistry) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cmd in registry.all():
        out.append(_build_command_schema(cmd.name, cmd.description))
    for router in registry.all_routers():
        out.append(_build_router_schema(router))
    return out


async def _push(guild_id: str | None) -> int:
    settings = get_settings()
    if not (settings.discord_application_id and settings.discord_bot_token):
        print(
            "ERROR: DISCORD_APPLICATION_ID + DISCORD_BOT_TOKEN must both be set in .env",
            file=sys.stderr,
        )
        return 1

    config = load_app_config(settings)

    # Build the registry exactly like main.py would so the manifest
    # reflects what the running bot will actually accept (including
    # dynamic /service action subcommands derived from yaml).
    audit = AuditLogger(config.audit.path)
    if settings.bot_cmder_master_key:
        store = SecretStore(config.totp.secret_store_path, settings.bot_cmder_master_key)
        totp: TOTPVerifier | None = TOTPVerifier(store)
        pending: PendingOTPSessions | None = PendingOTPSessions(ttl_s=config.totp.session_ttl_s)
    else:
        totp = None
        pending = None
    ssh_pool = SshConnectorPool(config.hosts, config.ssh)
    registry = CommandRegistry()
    install_all(registry, pending=pending, totp=totp, audit=audit, ssh_pool=ssh_pool, config=config)

    manifest = build_manifest(registry)
    print(f"Manifest: {len(manifest)} top-level command(s):")
    for m in manifest:
        opts = m.get("options", [])
        opt_str = f" + {opts[0]['name']}" if opts else ""
        print(f"  /{m['name']}{opt_str} — {m['description']}")

    async with DiscordClient(
        bot_token=settings.discord_bot_token,
        application_id=settings.discord_application_id,
    ) as client:
        if guild_id:
            print(f"\nPushing to guild {guild_id} (instant propagation)...")
            response = await client.overwrite_guild_commands(guild_id, manifest)
        else:
            print("\nPushing globally (~1h propagation)...")
            response = await client.overwrite_global_commands(manifest)

    print(f"Discord accepted {len(response)} command(s).")
    return 0


def _resolve_guild(args: argparse.Namespace) -> str | None:
    """Pick the guild ID per documented precedence (see module docstring).

    Treats empty / whitespace-only `DISCORD_GUILD_ID` as unset —
    `DISCORD_GUILD_ID=` in `.env` (a placeholder line) must not push
    to a guild called "" (which would 404 noisily).
    """
    if args.global_:
        return None
    if args.guild:
        return args.guild.strip() or None
    env = (get_settings().discord_guild_id or "").strip()
    return env or None


def cmd_discord_register(args: argparse.Namespace) -> int:
    """Handler for `bot-cmder discord-register`. Wraps the async push."""
    return asyncio.run(_push(_resolve_guild(args)))


def add_subparsers(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "discord-register",
        help="Push slash command schema to Discord (one-time / after registry edits)",
        description=(
            "PUT-replace the slash command list on Discord with what the running "
            "bot supports. Run after adding/removing builtin commands or service "
            "actions. Idempotent."
        ),
    )
    scope = p.add_mutually_exclusive_group()
    scope.add_argument(
        "--guild",
        metavar="GUILD_ID",
        help=(
            "Push to one guild (instant propagation). Overrides DISCORD_GUILD_ID "
            "from .env. Get the ID from Discord client → Developer Mode → "
            "right-click server icon → Copy Server ID."
        ),
    )
    scope.add_argument(
        "--global",
        dest="global_",  # `global` is a Python keyword
        action="store_true",
        help=(
            "Force a global push even when DISCORD_GUILD_ID is set in .env. "
            "Use this for the prod push after dogfooding in a guild."
        ),
    )
    p.set_defaults(func=cmd_discord_register)
