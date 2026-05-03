"""Centralized credential-redaction for logs and chat echoes.

The motivating incident: a dogfood pass found that
`bot_cmder/core/dispatcher.py` was logging the raw `/otp 918493`
args to `var/audit.jsonl`, defeating the whole point of the Fernet-
encrypted SecretStore. Worse, when a user mistyped a TOTP enrollment
URI as the `/otp` argument, the BASE32-encoded secret landed in the
audit log in plaintext — anyone reading the file could regenerate
every future OTP for that user, bypassing encryption-at-rest.

Both audit and chat echoes (Slack does this) need the same
redaction logic. Centralizing here means adding a new sensitive
command (e.g. a future `/password`) is a one-line edit instead of
chasing every adapter + the dispatcher.

Two flavors, same root cause:

  - `redact_args_for_audit(name, args)` — for `audit.log(args=...)`
    calls. Replaces the args list with `["<redacted>"]` for sensitive
    commands. Keeps the audit row count + structure stable so jq
    queries don't need special-casing.

  - `redact_text(typed)` — for chat echoes (Slack) and console recv
    logs (Telegram / Discord routers + daemon). Rewrites the typed
    string `/cmd <code>` to `/cmd <redacted>` while leaving the
    command name visible — operators still see WHAT was issued, just
    not the secret.

The set of sensitive command names lives in one place
(`_SENSITIVE_COMMAND_NAMES`) so changes get reviewed centrally.
"""

from __future__ import annotations

# Add a name here when you introduce a builtin whose args are credentials.
# Keep extremely conservative — overlogging in one place is recoverable;
# leaking a credential into audit.jsonl is not.
_SENSITIVE_COMMAND_NAMES: frozenset[str] = frozenset({"otp"})

REDACTED_PLACEHOLDER = "<redacted>"


def is_sensitive_command(name: str) -> bool:
    """Used by adapters / dispatcher to decide whether to redact."""
    return name in _SENSITIVE_COMMAND_NAMES


def redact_args_for_audit(command_name: str, args: list[str]) -> list[str]:
    """Substitute placeholder for the entire args list of sensitive commands.

    We intentionally don't try to redact only "the credential-looking
    arg" — the whole args list goes. A more precise rule (e.g. "redact
    args[0] only") risks missing future shapes (`/otp <code> <reason>`)
    and offers no real upside: audit logs already record the command
    name and the user, which is all an operator needs to investigate.
    """
    if is_sensitive_command(command_name):
        return [REDACTED_PLACEHOLDER]
    return args


def redact_text(typed: str) -> str:
    """Rewrite `/cmd <args>` to `/cmd <redacted>` for sensitive commands.

    Used by chat echoes and console `recv` logs where the WHOLE typed
    text gets surfaced. Preserves the command name so operators can
    still see what was issued; replaces everything after the first
    space with the placeholder.

    Non-command text and non-sensitive commands pass through unchanged.
    """
    if not typed.startswith("/"):
        return typed
    space_idx = typed.find(" ")
    if space_idx == -1:
        return typed  # bare /cmd, no args to redact
    cmd_part = typed[:space_idx]  # "/otp"
    cmd_name = cmd_part[1:]
    if is_sensitive_command(cmd_name):
        return f"{cmd_part} {REDACTED_PLACEHOLDER}"
    return typed
