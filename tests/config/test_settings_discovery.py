"""Tests for `bot_cmder.config.settings.resolve_app_config_path` (issue #20).

Pin the search-order contract for app.yaml location:

    1. settings.bot_cmder_config (env: BOT_CMDER_CONFIG)
    2. settings.app_config_path  (env: APP_CONFIG_PATH) — DEPRECATED
    3. ./config/app.yaml         (CWD-relative, only if file exists)
    4. <config_dir()>/app.yaml   (XDG fallback)
    5. None                      → caller treats as "use defaults"
"""

from __future__ import annotations

import warnings

import pytest

from bot_cmder.config.settings import Settings, get_settings, resolve_app_config_path


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    """Strip every env var that could leak from the host into the
    resolution path; isolate CWD to a tmp dir."""
    for var in (
        "BOT_CMDER_CONFIG",
        "BOT_CMDER_CONFIG_DIR",
        "BOT_CMDER_STATE_DIR",
        "APP_CONFIG_PATH",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings(**kwargs) -> Settings:
    """Construct a fresh Settings without reading any .env file —
    test isolation requires that.

    Pass overrides like `bot_cmder_config=Path(...)` or
    `app_config_path=Path(...)`.
    """
    return Settings(_env_file=None, **kwargs)


def test_bot_cmder_config_env_wins(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit-app.yaml"
    explicit.write_text("users: []\n")
    monkeypatch.setenv("BOT_CMDER_CONFIG", str(explicit))
    s = Settings(_env_file=None)
    assert resolve_app_config_path(s) == explicit


def test_app_config_path_env_emits_deprecation_and_returns_path(tmp_path, monkeypatch):
    """`APP_CONFIG_PATH` keeps working but warns. Critical: the warning
    must surface to callers (DeprecationWarning), not just go to log."""
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text("users: []\n")
    monkeypatch.setenv("APP_CONFIG_PATH", str(legacy))
    s = Settings(_env_file=None)

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        path = resolve_app_config_path(s)

    assert path == legacy
    deprecations = [w for w in captured if issubclass(w.category, DeprecationWarning)]
    assert any(
        "APP_CONFIG_PATH" in str(w.message) for w in deprecations
    ), f"expected DeprecationWarning mentioning APP_CONFIG_PATH; got {[str(w.message) for w in captured]}"


def test_bot_cmder_config_wins_over_legacy_alias(tmp_path, monkeypatch):
    """When both new and legacy env vars are set, the new one wins —
    so an SRE migrating their `.env` doesn't get bitten by leaving the
    old line in by mistake."""
    new = tmp_path / "new.yaml"
    new.write_text("users: []\n")
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text("users: []\n")
    monkeypatch.setenv("BOT_CMDER_CONFIG", str(new))
    monkeypatch.setenv("APP_CONFIG_PATH", str(legacy))
    s = Settings(_env_file=None)
    assert resolve_app_config_path(s) == new


def test_cwd_config_yaml_used_when_no_env(tmp_path, monkeypatch):
    cwd_yaml = tmp_path / "work" / "config" / "app.yaml"
    cwd_yaml.parent.mkdir()
    cwd_yaml.write_text("users: []\n")
    s = Settings(_env_file=None)
    assert resolve_app_config_path(s) == cwd_yaml.resolve()


def test_xdg_yaml_used_when_no_cwd(tmp_path, monkeypatch):
    """Installed-user case: no env override, no `./config/app.yaml`,
    fall through to `$XDG_CONFIG_HOME/bot-cmder/app.yaml`."""
    xdg = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    cfg = xdg / "bot-cmder"
    cfg.mkdir(parents=True)
    yaml_path = cfg / "app.yaml"
    yaml_path.write_text("users: []\n")
    s = Settings(_env_file=None)
    assert resolve_app_config_path(s) == yaml_path


def test_returns_none_when_nothing_found():
    """No env, no CWD config, no XDG config → None. `load_app_config`
    catches this and returns built-in `AppConfig()` defaults."""
    s = Settings(_env_file=None)
    assert resolve_app_config_path(s) is None
