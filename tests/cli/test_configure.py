"""Tests for `bot_cmder.cli.configure` (issue #45).

Skeleton-level coverage (C3 of the issue):

  1. `bot-cmder configure --help` lists the positional + options
  2. Missing .env → exit 1 with operator-facing message pointing at `init`
  3. .env present but no BOT_CMDER_MASTER_KEY → exit 1 with similar message
  4. --non-interactive + no positional → exit 1 (would have prompted for menu)
  5. Direct mode with stub flow returns 0 + prints "no changes to write"

Per-platform flow tests (Telegram / Discord / Slack walkthroughs +
prompt-driven keystroke patterns) land in C4 / C5 / C6 of the issue
once `_flow_<platform>` does real work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot_cmder.cli import main


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Strip env vars that would change config_dir() resolution; same
    pattern as tests/cli/test_init.py."""
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


def _seed_env(cfg_dir: Path, *, with_master_key: bool = True) -> Path:
    """Write a minimal `.env` shaped like `bot-cmder init` would, so
    the configure preconditions (file exists + master key set) pass."""
    cfg_dir.mkdir(parents=True, exist_ok=True)
    env_path = cfg_dir / ".env"
    body = ""
    if with_master_key:
        body += "BOT_CMDER_MASTER_KEY=fake-fernet-key-for-testing-only=\n"
    body += "# TELEGRAM_TOKEN=...\n"
    env_path.write_text(body, encoding="utf-8")
    return env_path


# --- 1. --help lists subcommand + flags --------------------------------


def test_configure_help_lists_positional_and_flags(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["configure", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    # Positional
    assert "telegram" in out
    assert "discord" in out
    assert "slack" in out
    assert "all" in out
    # Flags
    assert "--config-dir" in out
    assert "--dry-run" in out
    assert "--non-interactive" in out


# --- 2. .env missing -> exit 1 ----------------------------------------


def test_missing_env_exits_with_init_hint(tmp_path, capsys):
    cfg = tmp_path / "fresh-cfg"
    cfg.mkdir()
    # No .env at all
    rc = main(["configure", "--config-dir", str(cfg), "--non-interactive"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no .env" in err
    assert "bot-cmder init" in err


# --- 3. Master key missing -> exit 1 ----------------------------------


def test_env_without_master_key_exits_with_init_hint(tmp_path, capsys):
    cfg = tmp_path / "broken-cfg"
    _seed_env(cfg, with_master_key=False)
    rc = main(["configure", "--config-dir", str(cfg), "--non-interactive"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "BOT_CMDER_MASTER_KEY missing" in err
    assert "bot-cmder init" in err or "gen-master-key" in err


# --- 4. Menu mode without TTY -> exit 1 -------------------------------


def test_menu_mode_non_interactive_refuses(tmp_path, capsys):
    """When the operator omits the positional, the wizard wants to
    show the menu — and refuses if --non-interactive is set."""
    cfg = tmp_path / "cfg"
    _seed_env(cfg)
    rc = main(["configure", "--config-dir", str(cfg), "--non-interactive"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "non-interactive" in err
    assert "platform picker" in err or "would have prompted" in err


# --- 5. Direct mode with stub flow -> 0 + "no changes" ----------------


def test_direct_mode_with_stub_flow_returns_zero(tmp_path, capsys):
    """C3 ships `_flow_<platform>` as stubs that return False (no
    changes). The dispatcher should propagate that as a clean
    'no changes to write' exit. C4-C6 replace each stub; this test
    will keep passing because the stub's contract is `returns False`
    when no edits happened (which IS what an idempotent flow with
    no changes does too)."""
    cfg = tmp_path / "cfg"
    env_path = _seed_env(cfg)
    before = env_path.read_text(encoding="utf-8")

    rc = main(["configure", "telegram", "--config-dir", str(cfg), "--non-interactive"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No changes to write" in out

    # File untouched
    assert env_path.read_text(encoding="utf-8") == before
