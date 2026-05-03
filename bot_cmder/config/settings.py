from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from bot_cmder.config.schema import AppConfig


class Settings(BaseSettings):
    """Environment-backed settings.

    Secrets and per-environment knobs come from `.env` (or real env vars).
    Structure (users, ACL, healthcheck targets) lives in the YAML file
    pointed at by `app_config_path`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
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

    app_config_path: Path = Path("./config/app.yaml")

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
    #     script** (scripts/register_discord_commands.py), never by
    #     the running bot. When set, slash command registration scopes
    #     to that one guild (instant propagation, dev default); when
    #     unset, registration goes global (~1h propagation, prod). The
    #     `--guild=<id>` CLI flag overrides this for one-off pushes.
    discord_public_key: str | None = None
    discord_bot_token: str | None = None
    discord_application_id: str | None = None
    discord_guild_id: str | None = None

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

    # Read ONLY by scripts/register_slack_commands.py (the running bot
    # never touches this — Slack tells the bot where the request came
    # from per-call). Public HTTPS URL Slack should POST slash commands
    # to. Bare hostname is fine; the script appends /webhooks/slack.
    # Falls back to NGROK_DOMAIN if unset, so dev iteration is terse.
    slack_request_url: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def load_app_config(settings: Settings | None = None) -> AppConfig:
    """Load AppConfig from YAML; return defaults if file is absent."""
    settings = settings or get_settings()
    path = settings.app_config_path
    if not path.exists():
        return AppConfig()
    config = AppConfig.from_yaml(path)
    if settings.audit_path is not None:
        config.audit.path = settings.audit_path
    return config
