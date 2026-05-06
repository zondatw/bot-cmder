from __future__ import annotations

import logging
import warnings
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from bot_cmder.config.paths import config_dir, env_file_path
from bot_cmder.config.schema import AppConfig

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Environment-backed settings.

    Secrets and per-environment knobs come from `.env` (or real env vars).
    Structure (users, ACL, healthcheck targets) lives in the YAML file
    pointed at by `app_config_path`.
    """

    # `env_file` is set dynamically per-instantiation in `get_settings()`
    # (issue #20) so we can search CWD `./.env` first, fall through to
    # `$XDG_CONFIG_HOME/bot-cmder/.env`, then None. Static "./.env" here
    # used to be the contract; that contract was CWD-only and broke for
    # `pip install bot-cmder` users running from anywhere.
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_token: str | None = None
    telegram_webhook_secret: str | None = None

    # Phase 6a — ingestion mode for the Telegram adapter.
    #   - "webhook" (default): bot exposes POST /webhooks/telegram and
    #     Telegram POSTs each update there. Requires public HTTPS URL.
    #   - "polling":  bot dials OUT to api.telegram.org and long-polls
    #     getUpdates. No public URL needed; ideal for home labs / NAT.
    #     The two modes are mutually exclusive in Telegram's API
    #     (getUpdates returns 409 if a webhook is registered); the
    #     daemon calls deleteWebhook at startup so the switch is
    #     automatic.
    # Anything other than "webhook" / "polling" fails fast at startup.
    telegram_mode: str = "webhook"
    # Long-poll wait (seconds) the daemon asks Telegram to hold the
    # connection open. Telegram caps at 50; 25 keeps idle connections
    # under most middleboxes' silent-drop window while still
    # significantly reducing request volume vs short polling.
    telegram_polling_timeout_s: int = 25
    # When flipping to polling, drop any updates Telegram queued
    # while a webhook was active. Useful during dev mode flapping
    # (don't replay yesterday's tests on first start). False keeps
    # the queued updates so a brief webhook outage doesn't lose them.
    telegram_polling_drop_pending: bool = False

    # Issue #20: `BOT_CMDER_CONFIG` is the canonical name; `APP_CONFIG_PATH`
    # is honored as a deprecated alias for one minor release with a
    # warning (`resolve_app_config_path()` below). Both default None so
    # callers can detect "user explicitly set this" vs "fall back to the
    # search order" — pre-#20 this field defaulted to `./config/app.yaml`,
    # which made the latter detection impossible.
    bot_cmder_config: Path | None = None
    app_config_path: Path | None = None

    audit_path: Path | None = None

    # Phase 2 — symmetric key used to encrypt TOTP secrets in the
    # SQLite store at rest. 32 url-safe base64 bytes, generated once
    # via `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`.
    # Required when any privileged command exists; the dispatcher
    # refuses to enable the OTP gate without it.
    bot_cmder_master_key: str | None = None

    # Phase 4 — Discord adapter. All three are needed together; the
    # adapter mounts /webhooks/discord only when every value is set.
    #   - DISCORD_PUBLIC_KEY: hex-encoded Ed25519 verify key from the
    #     Discord application's "General Information" page. Used to
    #     verify every incoming interaction; no key → adapter disabled.
    #   - DISCORD_BOT_TOKEN: bot token from "Bot" page. Used to PATCH
    #     deferred replies via Discord's webhook API.
    #   - DISCORD_APPLICATION_ID: numeric application ID (URL of the
    #     application page). Part of the follow-up URL we PATCH.
    #   - DISCORD_GUILD_ID: optional, **read only by the register
    #     subcommand** (`bot-cmder discord-register`), never by
    #     the running bot. When set, slash command registration scopes
    #     to that one guild (instant propagation, dev default); when
    #     unset, registration goes global (~1h propagation, prod). The
    #     `--guild=<id>` CLI flag overrides this for one-off pushes.
    discord_public_key: str | None = None
    discord_bot_token: str | None = None
    discord_application_id: str | None = None
    discord_guild_id: str | None = None

    # Phase 6c — ingestion mode for the Discord adapter.
    #   - "interactions" (default): bot exposes POST /webhooks/discord
    #     and Discord POSTs slash commands there. Requires public
    #     HTTPS URL + DISCORD_PUBLIC_KEY for Ed25519 verification.
    #     This is Phase 4 behavior, preserved as the default.
    #   - "gateway": bot opens a WebSocket OUT to Discord and chat
    #     events arrive over that. NO public URL needed; ideal for
    #     home labs / NAT. **Slash commands DON'T arrive over the
    #     Gateway** (Discord platform limitation) — UX shifts to
    #     `@bot cmd args` in guild channels or plain `cmd args` in
    #     DMs. Requires DISCORD_BOT_TOKEN + the MESSAGE_CONTENT
    #     privileged intent enabled in the Discord dev portal.
    # Anything other than "interactions" / "gateway" fails fast at
    # startup. The two modes are mutually exclusive per instance.
    discord_mode: str = "interactions"

    # Phase 5 — Slack adapter. Both required for the adapter to mount;
    # without either, /webhooks/slack stays absent and Slack is silently
    # disabled (logged at WARNING during startup).
    #   - SLACK_SIGNING_SECRET: hex string from the Slack app's "Basic
    #     Information" page → "App Credentials" → "Signing Secret".
    #     Used to verify every incoming slash command via
    #     HMAC-SHA256(`v0:<ts>:<body>`); Slack rejects endpoints that
    #     accept unsigned payloads.
    #   - SLACK_BOT_TOKEN: bot token from "OAuth & Permissions" page
    #     (xoxb-...). Currently unused for replies (per-request
    #     `response_url` doesn't need bot auth) but reserved for Phase
    #     6 socket mode + future Block Kit posting.
    # Reply visibility tuning (ephemeral vs in_channel) lives in
    # `config/app.yaml` under `slack:`, not here — it's behavior, not
    # secrets.
    slack_signing_secret: str | None = None
    slack_bot_token: str | None = None

    # Phase 6b — ingestion mode for the Slack adapter.
    #   - "events" (default): bot exposes POST /webhooks/slack and
    #     Slack POSTs slash commands there. Requires public HTTPS URL +
    #     SLACK_SIGNING_SECRET for HMAC verification.
    #   - "socket":  bot opens a WebSocket OUT to Slack and slash
    #     commands arrive over that. NO public URL needed; ideal for
    #     home labs / NAT / restrictive corp egress. Requires
    #     SLACK_APP_TOKEN (xapp-...) instead of (or in addition to)
    #     the signing secret. Slack's UI enforces app-side mutex —
    #     when Socket Mode is enabled, the Events API URL field is
    #     greyed out — so the env mode just needs to match the app
    #     config.
    # Anything other than "events" / "socket" fails fast at startup.
    slack_mode: str = "events"
    # App-level token from Slack app config → Basic Information →
    # "App-Level Tokens" → Generate (scope: connections:write).
    # Distinct from SLACK_BOT_TOKEN (xoxb-...): the app token (xapp-...)
    # authorizes opening a Socket Mode connection; the bot token
    # authorizes acting as the bot user. Required when SLACK_MODE=socket.
    slack_app_token: str | None = None

    # Read ONLY by `bot-cmder slack-manifest` (the running bot
    # never touches this — Slack tells the bot where the request came
    # from per-call). Public HTTPS URL Slack should POST slash commands
    # to. Bare hostname is fine; the subcommand appends /webhooks/slack.
    # Falls back to NGROK_DOMAIN if unset, so dev iteration is terse.
    # Ignored entirely in SLACK_MODE=socket.
    slack_request_url: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build a Settings instance with dynamic .env discovery.

    pydantic-settings reads env vars + an optional `.env`. We resolve
    the .env path at instantiation time so the search order honors CWD
    → XDG. Static `env_file=".env"` would break for `pip install`
    users running from any CWD that doesn't happen to contain a .env.
    """
    env_file = env_file_path()
    return Settings(_env_file=env_file) if env_file else Settings()


def resolve_app_config_path(settings: Settings | None = None) -> Path | None:
    """Resolve the app.yaml location, walking the issue #20 search order.

    Order (CWD wins over XDG by design — keeps the dev workflow
    identical to Phase 1-7):

      1. settings.bot_cmder_config (env: BOT_CMDER_CONFIG)
      2. settings.app_config_path (env: APP_CONFIG_PATH) — DEPRECATED,
         emits a warning when used
      3. ./config/app.yaml (CWD-relative, only if it exists)
      4. <config_dir()>/app.yaml (default $XDG_CONFIG_HOME/bot-cmder/app.yaml)
      5. None — caller treats as "use AppConfig() built-in defaults"
    """
    settings = settings or get_settings()
    if settings.bot_cmder_config is not None:
        return settings.bot_cmder_config
    if settings.app_config_path is not None:
        warnings.warn(
            "APP_CONFIG_PATH is deprecated; use BOT_CMDER_CONFIG. "
            "Both names are honored in 0.2.x; 0.3.0 will drop APP_CONFIG_PATH.",
            DeprecationWarning,
            stacklevel=2,
        )
        logger.warning("APP_CONFIG_PATH is deprecated; use BOT_CMDER_CONFIG (will be removed in 0.3.0)")
        return settings.app_config_path
    cwd_yaml = Path("./config/app.yaml").resolve()
    if cwd_yaml.is_file():
        return cwd_yaml
    xdg_yaml = config_dir() / "app.yaml"
    if xdg_yaml.is_file():
        return xdg_yaml
    return None


def load_app_config(settings: Settings | None = None) -> AppConfig:
    """Load AppConfig from YAML; return defaults if no file is found."""
    settings = settings or get_settings()
    path = resolve_app_config_path(settings)
    if path is None or not path.exists():
        return AppConfig()
    config = AppConfig.from_yaml(path)
    if settings.audit_path is not None:
        config.audit.path = settings.audit_path
    return config
