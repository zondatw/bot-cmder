"""TOTP-management subcommands for `bot-cmder`.

Three commands, all out-of-band of the running bot:

    bot-cmder enroll-totp --user telegram:111
    bot-cmder list-totp
    bot-cmder revoke-totp --user telegram:111

Reads the same Settings / AppConfig as the FastAPI app, so the CLI
and the running bot share the same SecretStore on disk. The
SecretStore path resolves through `state_dir()` (issue #20), so
prod-style users get `~/.local/state/bot-cmder/totp.sqlite` while
source contributors with a `./var/` keep using that.
"""

from __future__ import annotations

import argparse
import sys

from bot_cmder.auth.secret_store import SecretStore
from bot_cmder.auth.totp import TOTPVerifier
from bot_cmder.config.schema import AppConfig
from bot_cmder.config.settings import Settings, get_settings, load_app_config


def _open_store(settings: Settings, config: AppConfig) -> SecretStore | None:
    if not settings.bot_cmder_master_key:
        print(
            "ERROR: BOT_CMDER_MASTER_KEY is not set. Generate one with:\n"
            "  bot-cmder gen-master-key\n"
            "Then add it to .env and re-run.",
            file=sys.stderr,
        )
        return None
    return SecretStore(config.totp.secret_store_path, settings.bot_cmder_master_key)


def cmd_enroll_totp(args: argparse.Namespace) -> int:
    settings = get_settings()
    config = load_app_config(settings)
    store = _open_store(settings, config)
    if store is None:
        return 1
    verifier = TOTPVerifier(store)
    secret, uri = verifier.enroll(args.user)
    print(f"Enrolled: {args.user}")
    print()
    print("Manual entry secret (base32):")
    print(f"  {secret}")
    print()
    print("Provisioning URI (use any QR generator, or paste into 1Password / Authy):")
    print(f"  {uri}")
    print()
    print("Render the QR in your terminal with one of:")
    print(f"  echo '{uri}' | qrencode -t ANSI -o -")
    print(f"  echo '{uri}' | python -m qrcode")
    print()
    print("Note: any previous TOTP enrollment for this user has been replaced.")
    return 0


def cmd_list_totp(args: argparse.Namespace) -> int:
    settings = get_settings()
    config = load_app_config(settings)
    store = _open_store(settings, config)
    if store is None:
        return 1
    users = store.list_users()
    if not users:
        print("No users enrolled.")
    else:
        print(f"Enrolled users ({len(users)}):")
        for u in users:
            print(f"  {u}")
    return 0


def cmd_revoke_totp(args: argparse.Namespace) -> int:
    settings = get_settings()
    config = load_app_config(settings)
    store = _open_store(settings, config)
    if store is None:
        return 1
    if store.delete_secret(args.user):
        print(f"Revoked TOTP for {args.user}")
        return 0
    print(f"User {args.user!r} was not enrolled.", file=sys.stderr)
    return 1


def add_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register `enroll-totp`, `list-totp`, `revoke-totp` on the parent
    dispatcher's subparser group. The parent CLI calls this exactly
    once at startup; each subcommand stashes its handler via
    `set_defaults(func=...)` so the dispatcher's `args.func(args)`
    routes correctly."""
    p_enroll = sub.add_parser("enroll-totp", help="Generate and store a TOTP secret for a user")
    p_enroll.add_argument("--user", required=True, help="Normalized user id, e.g. telegram:111")
    p_enroll.set_defaults(func=cmd_enroll_totp)

    p_list = sub.add_parser("list-totp", help="List all enrolled users")
    p_list.set_defaults(func=cmd_list_totp)

    p_revoke = sub.add_parser("revoke-totp", help="Delete a user's TOTP enrollment")
    p_revoke.add_argument("--user", required=True)
    p_revoke.set_defaults(func=cmd_revoke_totp)
