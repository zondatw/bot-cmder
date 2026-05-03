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
    # Guild scoping for slash command registration is a one-shot
    # registration-time concern (not runtime) and lives as a CLI
    # flag on `register_discord_commands.py`, not as an env var.
    discord_public_key: str | None = None
    discord_bot_token: str | None = None
    discord_application_id: str | None = None


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
