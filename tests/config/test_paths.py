"""Tests for `bot_cmder.config.paths` (issue #20).

Pin the resolution contract that schema.py defaults + settings.py
search order rely on. The same logic governs where audit.jsonl,
totp.sqlite, app.yaml and .env land for both dev (CWD wins) and
installed (XDG fallback) deployments — getting the order wrong
silently moves SRE tooling state to a different directory, which is
the kind of bug that surfaces months later under "I lost my TOTP
enrollments after updating".
"""

from __future__ import annotations

from pathlib import Path

from bot_cmder.config.paths import config_dir, env_file_path, state_dir

# --- state_dir ----------------------------------------------------------


def test_state_dir_explicit_env_wins(monkeypatch, tmp_path):
    """`BOT_CMDER_STATE_DIR` always wins, no fallthrough."""
    explicit = tmp_path / "custom-state"
    monkeypatch.setenv("BOT_CMDER_STATE_DIR", str(explicit))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-not-used"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "var").mkdir()  # CWD ./var/ also present — must be ignored
    assert state_dir() == explicit


def test_state_dir_cwd_var_wins_over_xdg(monkeypatch, tmp_path):
    """When `./var/` exists in CWD, it beats XDG. Preserves the dev
    workflow (`cd ~/dev/bot-cmder && bot-cmder serve` keeps writing
    state to the repo's `./var/`)."""
    monkeypatch.delenv("BOT_CMDER_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "var").mkdir()
    assert state_dir() == (tmp_path / "var").resolve()


def test_state_dir_xdg_when_no_cwd_var(monkeypatch, tmp_path):
    """No CWD `./var/` → fall through to `$XDG_STATE_HOME/bot-cmder/`."""
    monkeypatch.delenv("BOT_CMDER_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    # No ./var/ in CWD intentionally
    assert state_dir() == tmp_path / "xdg" / "bot-cmder"


def test_state_dir_default_when_no_xdg(monkeypatch, tmp_path):
    """No CWD `./var/`, no `XDG_STATE_HOME` → `~/.local/state/bot-cmder/`."""
    monkeypatch.delenv("BOT_CMDER_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path / ".unrelated") if (tmp_path / ".unrelated").is_dir() else monkeypatch.chdir(tmp_path)
    # Use a fresh subdir as CWD that doesn't have ./var/
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")
    assert state_dir() == tmp_path / ".local" / "state" / "bot-cmder"


# --- config_dir ---------------------------------------------------------


def test_config_dir_explicit_env_wins(monkeypatch, tmp_path):
    explicit = tmp_path / "custom-config"
    monkeypatch.setenv("BOT_CMDER_CONFIG_DIR", str(explicit))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-not-used"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    assert config_dir() == explicit


def test_config_dir_cwd_config_wins_over_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("BOT_CMDER_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    assert config_dir() == (tmp_path / "config").resolve()


def test_config_dir_xdg_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("BOT_CMDER_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    assert config_dir() == tmp_path / "xdg" / "bot-cmder"


def test_config_dir_default_when_no_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("BOT_CMDER_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    assert config_dir() == tmp_path / ".config" / "bot-cmder"


# --- env_file_path ------------------------------------------------------


def test_env_file_returns_cwd_when_present(monkeypatch, tmp_path):
    """`./.env` in CWD wins — preserves dev workflow exactly."""
    monkeypatch.delenv("BOT_CMDER_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    cwd_env = tmp_path / ".env"
    cwd_env.write_text("X=1\n")
    assert env_file_path() == cwd_env.resolve()


def test_env_file_falls_through_to_config_dir(monkeypatch, tmp_path):
    """No CWD .env → look in config_dir() (XDG_CONFIG_HOME/bot-cmder/.env)."""
    monkeypatch.delenv("BOT_CMDER_CONFIG_DIR", raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    cfg = xdg / "bot-cmder"
    cfg.mkdir(parents=True)
    (cfg / ".env").write_text("X=1\n")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    assert env_file_path() == cfg / ".env"


def test_env_file_returns_none_when_neither_exists(monkeypatch, tmp_path):
    """No CWD .env, no XDG .env → None (NOT a stale path that pydantic
    would warn about)."""
    monkeypatch.delenv("BOT_CMDER_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-xdg"))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    assert env_file_path() is None


# --- regression / type guarantees ---------------------------------------


def test_all_helpers_return_path():
    """Sanity: every helper returns a `Path` (or None for env_file_path),
    not a str. Downstream code does Path-method dispatches like
    `.is_file()` — strings would silently break those."""
    assert isinstance(state_dir(), Path)
    assert isinstance(config_dir(), Path)
    result = env_file_path()
    assert result is None or isinstance(result, Path)
