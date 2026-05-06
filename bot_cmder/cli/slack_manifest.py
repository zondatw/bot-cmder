"""`bot-cmder slack-manifest` — generate the Slack app manifest from the live registry.

Run after editing the registry (adding a builtin, renaming, changing
a description) so Slack's slash command list matches what the bot
can actually handle. Reads the registry exactly the same way `main.py`
does, so the manifest reflects what the running bot accepts —
including any dynamic /service action subcommands derived from yaml.

Why no `--push` mode (for now): unlike Discord, Slack's slash commands
can only be updated via `apps.manifest.update`, which requires
config tokens that rotate on every use (refresh-token flow, plus
persisting the new refresh token somewhere safe). That's significant
extra setup vs Discord's single bot-token PUT. Until the friction is
worth it, this script just GENERATES the manifest — paste the output
into Slack app config → Features → App Manifest tab → Save → accept
the reinstall confirmation. No more hand-maintained manifest file
that drifts from the registry.

URL precedence (highest wins):

  1. --request-url <url> CLI flag
  2. SLACK_REQUEST_URL env var
  3. Composed from NGROK_DOMAIN env var (https://{NGROK_DOMAIN}/webhooks/slack)
  4. Error out — Slack rejects manifests with placeholder URLs

The URL must end in `/webhooks/slack`; we append the suffix if you
pass just the bare hostname (`my-tunnel.ngrok-free.dev`) so dev
iteration stays terse.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import yaml

from bot_cmder.audit.log import AuditLogger
from bot_cmder.auth.pending import PendingOTPSessions
from bot_cmder.auth.secret_store import SecretStore
from bot_cmder.auth.totp import TOTPVerifier
from bot_cmder.commands.builtin import install_all
from bot_cmder.config.settings import get_settings, load_app_config
from bot_cmder.connectors.ssh import SshConnectorPool
from bot_cmder.core.registry import CommandRegistry, Router

# Slack manifest field caps — over-long descriptions are silently
# truncated by Slack's UI but the manifest itself rejects them at
# load time, so trim defensively.
_SLACK_DESC_MAX = 2000

WEBHOOK_PATH = "/webhooks/slack"


def _build_command_entry(name: str, description: str, request_url: str) -> dict[str, Any]:
    return {
        "command": f"/{name}",
        "url": request_url,
        "description": (description or name)[:_SLACK_DESC_MAX],
        "should_escape": False,
    }


def _build_router_entry(router: Router, request_url: str) -> dict[str, Any]:
    sub_count = len(router.subcommand_names())
    desc = f"{router.description} ({sub_count} subcommands — try /{router.name} help)"
    return {
        "command": f"/{router.name}",
        "url": request_url,
        "description": desc[:_SLACK_DESC_MAX],
        "should_escape": False,
    }


def build_manifest(
    registry: CommandRegistry,
    *,
    request_url: str,
    app_name: str = "bot-cmder",
) -> dict[str, Any]:
    slash_commands: list[dict[str, Any]] = []
    for cmd in registry.all():
        slash_commands.append(_build_command_entry(cmd.name, cmd.description, request_url))
    for router in registry.all_routers():
        slash_commands.append(_build_router_entry(router, request_url))

    return {
        "display_information": {"name": app_name},
        "features": {
            "bot_user": {"display_name": app_name},
            "slash_commands": slash_commands,
        },
        "oauth_config": {
            "scopes": {
                # `commands`: required for any /cmd to fire.
                # `chat:write`: reserved for Phase 6 Block Kit posting;
                # harmless to declare now so we don't need a reinstall later.
                "bot": ["commands", "chat:write"],
            },
        },
        "settings": {
            "event_subscriptions": {
                "request_url": request_url,
                "bot_events": [],
            },
            # All three explicitly off — the manifest spec requires them
            # to be present; defaulting to `false` matches Phase 5 surface.
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }


def _normalize_request_url(raw: str) -> str:
    """Accept bare hostname / full URL with or without /webhooks/slack tail."""
    raw = raw.strip().rstrip("/")
    if not raw:
        return raw
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    if not raw.endswith(WEBHOOK_PATH):
        raw = f"{raw}{WEBHOOK_PATH}"
    return raw


def _resolve_request_url(args: argparse.Namespace) -> str | None:
    """CLI flag > SLACK_REQUEST_URL env > NGROK_DOMAIN env > None."""
    if args.request_url:
        return _normalize_request_url(args.request_url)
    settings = get_settings()
    explicit = (settings.slack_request_url or "").strip()
    if explicit:
        return _normalize_request_url(explicit)
    # NGROK_DOMAIN is the dev-time tunnel hostname; if you've set it
    # for Telegram you almost certainly want to reuse it for Slack.
    import os

    ngrok = (os.environ.get("NGROK_DOMAIN") or "").strip()
    if ngrok:
        return _normalize_request_url(ngrok)
    return None


def _generate(args: argparse.Namespace) -> int:
    request_url = _resolve_request_url(args)
    if not request_url:
        print(
            "ERROR: no request URL. Pass --request-url <url>, set SLACK_REQUEST_URL "
            "in .env, or set NGROK_DOMAIN if you're using `just tunnel-ngrok`.",
            file=sys.stderr,
        )
        return 1

    settings = get_settings()
    config = load_app_config(settings)

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

    manifest = build_manifest(registry, request_url=request_url, app_name=args.app_name)
    rendered = yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False, allow_unicode=True)

    if args.out:
        args.out.write(rendered)
        if args.out is not sys.stdout:
            print(
                f"Wrote manifest to {args.out.name} ({len(manifest['features']['slash_commands'])} commands).",
                file=sys.stderr,
            )
            print(
                "Paste it into your Slack app → Features → App Manifest → Save, "
                "then accept the reinstall confirmation.",
                file=sys.stderr,
            )
    else:
        sys.stdout.write(rendered)
    return 0


def cmd_slack_manifest(args: argparse.Namespace) -> int:
    """Handler for `bot-cmder slack-manifest`. Generates the Slack
    manifest YAML and writes to args.out (default stdout)."""
    return _generate(args)


def add_subparsers(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "slack-manifest",
        help="Generate Slack app manifest YAML (paste into app config)",
        description=(
            "Render the Slack app manifest YAML reflecting the running bot's "
            "registry. Paste into Slack app config → Features → App Manifest."
        ),
    )
    p.add_argument(
        "--request-url",
        metavar="URL",
        help=(
            "Public HTTPS URL Slack should POST to. Bare hostname OK "
            "(e.g. 'my-tunnel.ngrok-free.dev'); '/webhooks/slack' is "
            "appended automatically. Overrides SLACK_REQUEST_URL env."
        ),
    )
    p.add_argument(
        "--app-name",
        default="bot-cmder",
        help="Slack app display name (default: bot-cmder).",
    )
    p.add_argument(
        "--out",
        type=argparse.FileType("w"),
        default=sys.stdout,
        help="Write manifest to file instead of stdout.",
    )
    p.set_defaults(func=cmd_slack_manifest)
