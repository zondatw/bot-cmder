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
