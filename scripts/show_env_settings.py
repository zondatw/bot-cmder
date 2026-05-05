#!/usr/bin/env python3
"""Pretty-print the env vars the bot reads.

Secrets print as `<set>` / `<unset>` only — never the actual value
(an earlier bash version of this leaked tokens to the terminal).
Non-secret IDs and paths print as-is so an operator can spot a typo.
"""

from __future__ import annotations

import os

# (env_var_name, label, treat_as_secret)
_FIELDS: list[tuple[str, str, bool]] = [
    # Telegram
    ("TELEGRAM_TOKEN", "TELEGRAM_TOKEN", True),
    ("TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_WEBHOOK_SECRET", True),
    ("TELEGRAM_MODE", "TELEGRAM_MODE", False),
    ("TELEGRAM_POLLING_TIMEOUT_S", "TELEGRAM_POLLING_TIMEOUT_S", False),
    ("TELEGRAM_POLLING_DROP_PENDING", "TELEGRAM_POLLING_DROP_PENDING", False),
    ("TELEGRAM_HOOK_URL", "TELEGRAM_HOOK_URL", False),
    ("NGROK_DOMAIN", "NGROK_DOMAIN", False),
    # Discord
    ("DISCORD_PUBLIC_KEY", "DISCORD_PUBLIC_KEY", True),
    ("DISCORD_BOT_TOKEN", "DISCORD_BOT_TOKEN", True),
    ("DISCORD_APPLICATION_ID", "DISCORD_APPLICATION_ID", False),
    ("DISCORD_GUILD_ID", "DISCORD_GUILD_ID", False),
    ("DISCORD_MODE", "DISCORD_MODE", False),
    # Slack
    ("SLACK_MODE", "SLACK_MODE", False),
    ("SLACK_SIGNING_SECRET", "SLACK_SIGNING_SECRET", True),
    ("SLACK_APP_TOKEN", "SLACK_APP_TOKEN", True),
    ("SLACK_BOT_TOKEN", "SLACK_BOT_TOKEN", True),
    ("SLACK_REQUEST_URL", "SLACK_REQUEST_URL", False),
    # TOTP
    ("BOT_CMDER_MASTER_KEY", "BOT_CMDER_MASTER_KEY", True),
    # App
    ("APP_CONFIG_PATH", "APP_CONFIG_PATH", False),
    ("AUDIT_PATH", "AUDIT_PATH", False),
    ("BIND_HOST", "BIND_HOST", False),
    ("BIND_PORT", "BIND_PORT", False),
    ("RELOAD", "RELOAD", False),
]

_GROUPS: list[tuple[str, list[str]]] = [
    (
        "Telegram",
        [
            "TELEGRAM_TOKEN",
            "TELEGRAM_WEBHOOK_SECRET",
            "TELEGRAM_MODE",
            "TELEGRAM_POLLING_TIMEOUT_S",
            "TELEGRAM_POLLING_DROP_PENDING",
            "TELEGRAM_HOOK_URL",
            "NGROK_DOMAIN",
        ],
    ),
    (
        "Discord",
        [
            "DISCORD_PUBLIC_KEY",
            "DISCORD_BOT_TOKEN",
            "DISCORD_APPLICATION_ID",
            "DISCORD_GUILD_ID",
            "DISCORD_MODE",
        ],
    ),
    (
        "Slack",
        [
            "SLACK_MODE",
            "SLACK_SIGNING_SECRET",
            "SLACK_APP_TOKEN",
            "SLACK_BOT_TOKEN",
            "SLACK_REQUEST_URL",
        ],
    ),
    ("TOTP", ["BOT_CMDER_MASTER_KEY"]),
    ("App", ["APP_CONFIG_PATH", "AUDIT_PATH", "BIND_HOST", "BIND_PORT", "RELOAD"]),
]


def _render_value(name: str) -> str:
    is_secret = next((s for n, _, s in _FIELDS if n == name), False)
    raw = os.environ.get(name, "")
    if not raw:
        return "<unset>"
    if is_secret:
        return "<set>"
    return raw


def main() -> int:
    print("--- env settings ---")
    label_width = max(len(n) for n in (n for _, names in _GROUPS for n in names))
    for group_name, names in _GROUPS:
        print(f"  {group_name}")
        for n in names:
            print(f"    {n.ljust(label_width)}  {_render_value(n)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
