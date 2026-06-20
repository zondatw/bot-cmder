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
import re
import secrets
import sys
from collections.abc import Callable
from enum import Enum
from pathlib import Path

from bot_cmder.cli._envfile import EnvFile
from bot_cmder.config.paths import config_dir

# --- shared helpers ----------------------------------------------------

_PLATFORM_CHOICES = ("telegram", "discord", "slack", "all")


class _Aborted(Exception):
    """Operator picked `Abort` from the validation-failed menu. Carries
    the platform name as its message. Caught by `cmd_configure`, which
    restores that platform's pre-flow `env.lines` snapshot AND breaks
    the `configure all` loop (operator wants OUT, not "keep walking"
    — the `--abort` semantics in `docs/configure.md` promise both)."""


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


def _refuse_if_non_interactive(args: argparse.Namespace, *, reason: str) -> bool:
    """True if --non-interactive blocks a prompt that's about to fire.

    Called at any point a prompt would happen. Just checks the
    operator-set flag — does NOT auto-detect non-TTY stdin, because
    that breaks legitimate uses (piped input via `prompt_toolkit
    pipe_input`, monkeypatched questionary in tests). If the operator
    is genuinely running this in CI without a TTY, they should pass
    --non-interactive explicitly; otherwise questionary will error on
    its own when it tries to read the terminal.
    """
    if args.non_interactive:
        print(
            f"bot-cmder configure: --non-interactive set, would have prompted: {reason}",
            file=sys.stderr,
        )
        return True
    return False


# --- shared per-field helpers ------------------------------------------


class FieldChoice(str, Enum):
    """User's pick for an existing-key prompt. NEW is the synthetic
    value used when no prior value existed — `_apply_field` just
    skips the choice menu and prompts directly."""

    KEEP = "keep"
    REPLACE = "replace"
    CLEAR = "clear"


def _mask(value: str | None, *, tail: int = 4) -> str:
    """Display first 3 + ··· + last N chars of a secret value. For
    short values (under 10 chars), display ··· only. Used in the
    Keep/Replace/Clear menu so the operator can confirm WHICH token
    is there without revealing it in scrollback."""
    if not value:
        return "(unset)"
    if len(value) <= 10:
        return "···"
    return f"{value[:3]}···{value[-tail:]}"


def _prompt_existing_action(key: str, masked: str) -> FieldChoice:
    """Existing-value menu: Keep (default) / Replace / Clear.
    Lazy-imports questionary."""
    import questionary

    answer = questionary.select(
        f"{key} = {masked}",
        choices=[
            questionary.Choice("Keep current", value=FieldChoice.KEEP),
            questionary.Choice("Replace", value=FieldChoice.REPLACE),
            questionary.Choice("Clear (empty value)", value=FieldChoice.CLEAR),
        ],
    ).unsafe_ask()
    # Ctrl-C / Ctrl-D returns None — treat as Keep so the operator
    # doesn't accidentally clear something by interrupting mid-prompt
    return answer if isinstance(answer, FieldChoice) else FieldChoice.KEEP


def _prompt_value(
    prompt: str,
    *,
    secret: bool,
    validator: Callable[[str], bool | str] | None = None,
) -> str | None:
    """Prompt for a single value with optional regex/format validation.

    `validator(value)` returns True if OK, or an error string. None
    return from `unsafe_ask` (Ctrl-C) propagates as None so the caller
    can treat it as "abandon this field". Lazy-imports questionary.
    """
    import questionary

    widget = questionary.password if secret else questionary.text
    return widget(prompt, validate=validator).unsafe_ask()


def _apply_field(
    env: EnvFile,
    key: str,
    *,
    prompt: str,
    secret: bool = False,
    validator: Callable[[str], bool | str] | None = None,
    generator: Callable[[], str] | None = None,
) -> bool:
    """Walk one env field. Returns True iff env was modified.

    - If `env[key]` is set: show Keep/Replace/Clear menu (Keep default).
    - If unset: prompt directly. If `generator` provided, offer to
      auto-generate (e.g. webhook secret via secrets.token_urlsafe).
    - On Replace or new-value path, runs `validator` if provided.
    - Ctrl-C during a prompt returns the menu's default (Keep), so
      the operator doesn't accidentally lose a value.
    """
    import questionary

    existing = env.get(key)
    if existing:
        action = _prompt_existing_action(key, _mask(existing))
        if action == FieldChoice.KEEP:
            return False
        if action == FieldChoice.CLEAR:
            env.set(key, None)
            print(f"  Cleared {key}")
            return True
        # REPLACE — fall through to value prompt

    # Brand-new-value path. If a generator is offered, ask first.
    if (
        generator is not None
        and not existing
        and questionary.confirm(f"Auto-generate {key}?", default=True).unsafe_ask()
    ):
        value = generator()
        env.set(key, value)
        print(f"  Generated {key}: {_mask(value)}")
        return True

    value = _prompt_value(prompt, secret=secret, validator=validator)
    if value is None or value == "":
        print(f"  Skipped {key} (no value entered)")
        return False
    env.set(key, value)
    print(f"  Set {key}: {_mask(value)}")
    return True


def _make_regex_validator(pattern: str, hint: str) -> Callable[[str], bool | str]:
    """questionary `validate=` callback that runs a regex and returns
    True on match, or the operator-facing `hint` on miss. Empty input
    is allowed (skipped by `_apply_field`)."""
    compiled = re.compile(pattern)

    def _check(value: str) -> bool | str:
        if not value:
            return True  # empty = skip, handled in _apply_field
        if compiled.match(value):
            return True
        return hint

    return _check


# --- live API validation (issue #45 C7) --------------------------------
#
# Per-platform "is this token actually valid?" check using the
# platform's own auth endpoint. Opt-in via an in-flow questionary
# prompt at the end of each platform flow — default `No` so an
# offline run / fake-token testing path stays prompt-free.
#
# Each `_validate_<platform>` returns (ok, message): ok=True on
# success, False with a human-readable message on failure. Network
# errors and HTTP non-200 are both treated as failure.

# Match `bot_cmder.connectors.ssh` / the rest of the codebase —
# import httpx at top so the validator functions can be sync,
# called from the wizard's sync flow.

_VALIDATION_TIMEOUT_S = 5.0


def _validate_telegram(env: EnvFile) -> tuple[bool, str]:
    """`GET https://api.telegram.org/bot<TOKEN>/getMe` → expect
    `ok: true`. Telegram returns `ok: false, description: ...` on
    bad tokens; surface that as the failure message."""
    import httpx

    token = env.get("TELEGRAM_TOKEN")
    if not token:
        return False, "TELEGRAM_TOKEN not set — fill in the credentials first."
    try:
        r = httpx.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=_VALIDATION_TIMEOUT_S,
        )
    except httpx.HTTPError as e:
        return False, f"Network error: {e}"
    try:
        body = r.json()
    except Exception:
        return False, f"HTTP {r.status_code} with non-JSON body"
    if body.get("ok") is True:
        bot_name = body.get("result", {}).get("username", "<unknown>")
        return True, f"OK — bot is @{bot_name}"
    return False, f"Telegram says: {body.get('description', '<no description>')}"


def _validate_discord(env: EnvFile) -> tuple[bool, str]:
    """`GET https://discord.com/api/v10/users/@me` with `Authorization:
    Bot <TOKEN>`. Expect 200 + `bot: true`."""
    import httpx

    token = env.get("DISCORD_BOT_TOKEN")
    if not token:
        return False, "DISCORD_BOT_TOKEN not set — fill in the credentials first."
    try:
        r = httpx.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
            timeout=_VALIDATION_TIMEOUT_S,
        )
    except httpx.HTTPError as e:
        return False, f"Network error: {e}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    body = r.json()
    if body.get("bot") is True:
        return True, f"OK — bot user {body.get('username', '<unknown>')}"
    return False, "Discord returned a non-bot user — token may belong to a user account"


def _validate_slack(env: EnvFile) -> tuple[bool, str]:
    """`POST https://slack.com/api/auth.test` with `Authorization:
    Bearer <BOT_TOKEN>`. Expect 200 + `ok: true`."""
    import httpx

    token = env.get("SLACK_BOT_TOKEN")
    if not token:
        return False, "SLACK_BOT_TOKEN not set — fill in the credentials first."
    try:
        r = httpx.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_VALIDATION_TIMEOUT_S,
        )
    except httpx.HTTPError as e:
        return False, f"Network error: {e}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    body = r.json()
    if body.get("ok") is True:
        return True, f"OK — workspace {body.get('team', '<unknown>')} / user {body.get('user', '<unknown>')}"
    return False, f"Slack says: {body.get('error', '<no error>')}"


def _ask_validate(platform: str, validator: Callable[[EnvFile], tuple[bool, str]], env: EnvFile) -> str:
    """Ask `Validate against <platform> API?`, run validator if yes,
    and on failure offer Retry / Save anyway / Abort.

    Returns 'ok' for success / skipped / save-anyway. Raises
    `_Aborted(platform)` if the operator picks Abort — propagates up
    to `cmd_configure`, which restores the pre-flow env snapshot and
    stops walking subsequent platforms.
    """
    import questionary

    if not questionary.confirm(
        f"Validate {platform} credentials against the API now?",
        default=False,
    ).unsafe_ask():
        return "ok"

    while True:
        ok, msg = validator(env)
        if ok:
            print(f"  ✓ {msg}")
            return "ok"
        print(f"  ✗ {msg}")
        choice = questionary.select(
            "Validation failed. What now?",
            choices=[
                questionary.Choice("Retry (e.g. fixed token in another window)", value="retry"),
                questionary.Choice("Save anyway (I know what I'm doing)", value="save"),
                questionary.Choice("Abort (discard changes)", value="abort"),
            ],
        ).unsafe_ask()
        if choice == "retry":
            continue
        if choice == "abort":
            raise _Aborted(platform)
        return "ok"  # save-anyway path


# --- platform flows ----------------------------------------------------


def _flow_telegram(env: EnvFile, args: argparse.Namespace) -> bool:
    """Walk Telegram credential setup. Returns True if env was modified.

    Asks (in order): ingestion mode, bot token (required), webhook
    secret (webhook mode only — offers auto-generation). Polling-
    mode advanced knobs (timeout / drop_pending) deferred to a
    follow-up issue if anyone asks.
    """
    # cmd_configure already gated on _refuse_if_non_interactive; no
    # need to re-check here.
    import questionary

    print("\n=== Telegram ===")
    print(
        "  Setup docs: docs/telegram-polling.md (no-domain modes) +\n"
        "              docs/discord-setup.md style — get token from @BotFather"
    )

    changed = False

    # 1. Ingestion mode
    current_mode = env.get("TELEGRAM_MODE") or "webhook"
    mode = questionary.select(
        "Ingestion mode",
        choices=[
            questionary.Choice(
                title="webhook  — Telegram POSTs to your public URL (needs HTTPS)",
                value="webhook",
            ),
            questionary.Choice(
                title="polling  — Bot long-polls Telegram (no public URL needed)",
                value="polling",
            ),
        ],
        default=current_mode,
    ).unsafe_ask()
    if mode != env.get("TELEGRAM_MODE"):
        env.set("TELEGRAM_MODE", mode)
        changed = True

    # 2. Bot token (required for both modes)
    if _apply_field(
        env,
        "TELEGRAM_TOKEN",
        prompt="Bot token (from @BotFather, format `<digits>:<35+ chars>`)",
        secret=True,
        validator=_make_regex_validator(
            r"^\d+:[A-Za-z0-9_-]{30,}$",
            "Expected format: `<digits>:<35+ chars>` (e.g. `123456789:AAH-xxxxxxxxx...`)",
        ),
    ):
        changed = True

    # 3. Webhook-only: webhook secret with auto-gen option
    if mode == "webhook" and _apply_field(
        env,
        "TELEGRAM_WEBHOOK_SECRET",
        prompt="Webhook secret (any long random string)",
        secret=True,
        generator=lambda: secrets.token_urlsafe(32),
    ):
        changed = True

    # 4. Live validation (opt-in). Abort raises `_Aborted` which
    # `cmd_configure` catches to restore the pre-flow env snapshot;
    # save-anyway returns 'ok' so we fall through and the wizard
    # writes despite the failure.
    if changed:
        _ask_validate("Telegram", _validate_telegram, env)

    return changed


def _flow_discord(env: EnvFile, args: argparse.Namespace) -> bool:
    """Walk Discord credential setup. Returns True if env was modified.

    Asks (in order): ingestion mode, bot token, application ID,
    public key (interactions mode only — gateway doesn't sign-check),
    optional guild ID (read only by `bot-cmder discord-register`).
    """
    import questionary

    print("\n=== Discord ===")
    print(
        "  Setup docs: docs/discord-setup.md (interactions) +\n"
        "              docs/discord-gateway.md (gateway, no-domain mode)"
    )

    changed = False

    # 1. Ingestion mode
    current_mode = env.get("DISCORD_MODE") or "interactions"
    mode = questionary.select(
        "Ingestion mode",
        choices=[
            questionary.Choice(
                title="interactions  — Discord POSTs slash commands to your URL (needs HTTPS + public_key)",
                value="interactions",
            ),
            questionary.Choice(
                title="gateway       — Bot opens WSS (no URL, but loses slash commands)",
                value="gateway",
            ),
        ],
        default=current_mode,
    ).unsafe_ask()
    if mode != env.get("DISCORD_MODE"):
        env.set("DISCORD_MODE", mode)
        changed = True

    # 2. Bot token (required for both modes)
    if _apply_field(
        env,
        "DISCORD_BOT_TOKEN",
        prompt="Bot token (Discord dev portal → Bot → Reset Token)",
        secret=True,
    ):
        changed = True

    # 3. Application ID (required for both modes)
    if _apply_field(
        env,
        "DISCORD_APPLICATION_ID",
        prompt="Application ID (numeric, from app URL or General Information page)",
        secret=False,
        validator=_make_regex_validator(
            r"^\d{17,20}$",
            "Expected a 17-20 digit numeric snowflake ID",
        ),
    ):
        changed = True

    # 4. Public key — interactions mode only (gateway doesn't sign-check)
    if mode == "interactions" and _apply_field(
        env,
        "DISCORD_PUBLIC_KEY",
        prompt="Public key (64-char hex, from General Information page)",
        secret=False,
        validator=_make_regex_validator(
            r"^[0-9a-fA-F]{64}$",
            "Expected 64 hex characters (Discord public key format)",
        ),
    ):
        changed = True

    # 5. Guild ID — optional, only read by `bot-cmder discord-register`
    print(
        "  DISCORD_GUILD_ID is read ONLY by `bot-cmder discord-register`,\n"
        "  not by the running bot. Useful for dev-time slash command pushes."
    )
    if _apply_field(
        env,
        "DISCORD_GUILD_ID",
        prompt="Guild ID for dev slash command registration (optional, snowflake or blank)",
        secret=False,
        validator=_make_regex_validator(
            r"^\d{17,20}$",
            "Expected a 17-20 digit numeric snowflake ID (or leave blank to skip)",
        ),
    ):
        changed = True

    # Live validation (opt-in) — see _flow_telegram for abort/save semantics
    if changed:
        _ask_validate("Discord", _validate_discord, env)

    return changed


def _flow_slack(env: EnvFile, args: argparse.Namespace) -> bool:
    """Walk Slack credential setup. Returns True if env was modified.

    Asks (in order): ingestion mode, bot token, mode-specific
    credentials. Events mode needs signing_secret + optional
    request_url; socket mode needs app_token + optional
    signing_secret (Slack still surfaces signing for legacy events).
    """
    import questionary

    print("\n=== Slack ===")
    print(
        "  Setup docs: docs/slack-setup.md (events) +\n"
        "              docs/slack-socket-mode.md (socket, no-domain mode)"
    )

    changed = False

    # 1. Ingestion mode
    current_mode = env.get("SLACK_MODE") or "events"
    mode = questionary.select(
        "Ingestion mode",
        choices=[
            questionary.Choice(
                title="events  — Slack POSTs slash commands to your URL (needs HTTPS + signing_secret)",
                value="events",
            ),
            questionary.Choice(
                title="socket  — Bot opens WSS via app_token (no URL — home labs / corp egress)",
                value="socket",
            ),
        ],
        default=current_mode,
    ).unsafe_ask()
    if mode != env.get("SLACK_MODE"):
        env.set("SLACK_MODE", mode)
        changed = True

    # 2. Bot token (required for both modes)
    if _apply_field(
        env,
        "SLACK_BOT_TOKEN",
        prompt="Bot token (OAuth & Permissions → Bot User OAuth Token, starts with `xoxb-`)",
        secret=True,
        validator=_make_regex_validator(
            r"^xoxb-.+",
            "Expected to start with `xoxb-` (Slack bot user OAuth token)",
        ),
    ):
        changed = True

    # 3. Mode-specific fields
    if mode == "events":
        # Signing secret is mandatory for events mode (HMAC verifies
        # every inbound request)
        if _apply_field(
            env,
            "SLACK_SIGNING_SECRET",
            prompt="Signing secret (Basic Information → App Credentials, 32 hex chars)",
            secret=True,
            validator=_make_regex_validator(
                r"^[0-9a-fA-F]{32}$",
                "Expected 32 hex characters (Slack signing secret format)",
            ),
        ):
            changed = True
        # Optional: explicit request URL (otherwise slack-manifest
        # falls back to NGROK_DOMAIN). Read only by `bot-cmder
        # slack-manifest`, not by the running bot.
        print(
            "  SLACK_REQUEST_URL is read ONLY by `bot-cmder slack-manifest`,\n"
            "  not by the running bot. Leave blank to fall back to NGROK_DOMAIN."
        )
        if _apply_field(
            env,
            "SLACK_REQUEST_URL",
            prompt="Public HTTPS URL for slash commands (optional, bare host OK)",
            secret=False,
        ):
            changed = True
    else:  # socket
        # App token is mandatory for socket mode (opens the WSS conn)
        if _apply_field(
            env,
            "SLACK_APP_TOKEN",
            prompt="App-level token (Basic Information → App-Level Tokens, starts with `xapp-`)",
            secret=True,
            validator=_make_regex_validator(
                r"^xapp-.+",
                "Expected to start with `xapp-` (Slack app-level token)",
            ),
        ):
            changed = True
        # Signing secret is optional in socket mode (Slack still
        # surfaces it for legacy events APIs but bot doesn't use it
        # in pure socket mode)
        print("  SLACK_SIGNING_SECRET is optional in socket mode — leave blank to skip.")
        if _apply_field(
            env,
            "SLACK_SIGNING_SECRET",
            prompt="Signing secret (optional in socket mode, 32 hex chars)",
            secret=True,
            validator=_make_regex_validator(
                r"^[0-9a-fA-F]{32}$",
                "Expected 32 hex characters (or leave blank)",
            ),
        ):
            changed = True

    # Live validation (opt-in) — see _flow_telegram for abort/save semantics
    if changed:
        _ask_validate("Slack", _validate_slack, env)

    return changed


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
            if _refuse_if_non_interactive(args, reason="top-level platform picker"):
                return 1
            platform = _show_menu(env_path, env)
            if platform is None:
                print("Nothing to do — picked Quit.")
                return 0
        else:
            platform = args.platform

        # Dispatch each platform flow. Per-platform snapshot is the
        # rollback unit for `Abort` — restoring env.lines undoes ONLY
        # that platform's mutations, so a prior platform that
        # succeeded in `configure all` mode keeps its values. Break
        # after restore: an operator who picks Abort wants out, not
        # "keep walking the remaining platforms".
        any_changed = False
        flows_to_run = ("telegram", "discord", "slack") if platform == "all" else (platform,)
        for p in flows_to_run:
            if _refuse_if_non_interactive(args, reason=f"{p} credential flow"):
                return 1
            snapshot = list(env.lines)
            try:
                if _FLOWS[p](env, args):
                    any_changed = True
            except _Aborted as exc:
                env.lines = snapshot
                print(f"\nAborted {exc} — discarded its changes.", file=sys.stderr)
                break
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
