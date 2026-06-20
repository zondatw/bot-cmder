"""Tests for `bot-cmder init` (issue #20).

Two flow shapes to cover:

  1. Fresh init — no existing config, drops all three artifacts at
     the right paths with valid content. The fact that the master
     key actually decodes as a Fernet key matters; a typo in the
     template would silently break TOTP at first enrollment.

  2. Re-init — existing config plus the various --force / --rotate-key
     combinations. The default-preserve-master-key contract is the
     critical one: losing TOTP enrollments because of an accidental
     re-init is the footgun this whole subcommand is designed to
     prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from bot_cmder.cli import main


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Strip env vars that would change config_dir() / state_dir()
    resolution, isolate HOME so the XDG default lands in tmp_path."""
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


def _read_master_key_from_env(env_path: Path) -> str:
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("BOT_CMDER_MASTER_KEY="):
            return line.split("=", 1)[1]
    raise AssertionError(f"BOT_CMDER_MASTER_KEY not found in {env_path}")


def test_init_creates_three_artifacts(tmp_path, capsys):
    """Fresh `bot-cmder init --config-dir <tmp>` drops app.yaml + .env
    + state-dir. Each in the right shape."""
    cfg = tmp_path / "cfg"
    rc = main(["init", "--config-dir", str(cfg)])
    assert rc == 0

    app_yaml = cfg / "app.yaml"
    env_file = cfg / ".env"
    var_dir = cfg / "var"

    assert app_yaml.is_file(), "app.yaml missing"
    assert env_file.is_file(), ".env missing"
    assert var_dir.is_dir(), "state dir missing"

    # app.yaml round-trips through the schema (catches a typo in the
    # bundled example file, which would surface here on first init).
    from bot_cmder.config.schema import AppConfig

    AppConfig.from_yaml(app_yaml)

    # The .env's master key must be a valid Fernet key.
    master_key = _read_master_key_from_env(env_file)
    Fernet(master_key.encode())  # raises ValueError if invalid

    out = capsys.readouterr().out
    assert "Next steps:" in out
    assert "configure" in out  # issue #45 — new step
    assert "enroll-totp" in out


def test_init_chmods_env_to_600(tmp_path):
    """`.env` contains the master key — must be owner-rw only.
    Anything else is a leak surface."""
    cfg = tmp_path / "cfg"
    main(["init", "--config-dir", str(cfg)])
    mode = (cfg / ".env").stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600 perms on .env, got 0o{oct(mode)[2:]}"


def test_init_refuses_to_overwrite_without_force(tmp_path, capsys):
    """Existing app.yaml + no --force → exit 1, refuse, message
    explains the path. No silent clobbering of operator config."""
    cfg = tmp_path / "cfg"
    main(["init", "--config-dir", str(cfg)])

    rc = main(["init", "--config-dir", str(cfg)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to overwrite" in err
    assert "--force" in err


def test_init_force_preserves_existing_master_key_by_default(tmp_path):
    """`--force` overwrites app.yaml + .env BUT preserves the existing
    BOT_CMDER_MASTER_KEY — protects existing TOTP enrollments from
    being silently invalidated by a re-init."""
    cfg = tmp_path / "cfg"
    main(["init", "--config-dir", str(cfg)])
    original_key = _read_master_key_from_env(cfg / ".env")

    rc = main(["init", "--config-dir", str(cfg), "--force"])
    assert rc == 0

    new_key = _read_master_key_from_env(cfg / ".env")
    assert new_key == original_key, (
        "Re-running --force without --rotate-key must preserve the master key. "
        "Otherwise existing TOTP enrollments silently break."
    )


def test_init_rotate_key_regenerates(tmp_path):
    """`--rotate-key` (always combined with --force in spirit)
    regenerates the master key. Caller is opting INTO the destructive
    behavior."""
    cfg = tmp_path / "cfg"
    main(["init", "--config-dir", str(cfg)])
    original_key = _read_master_key_from_env(cfg / ".env")

    rc = main(["init", "--config-dir", str(cfg), "--force", "--rotate-key"])
    assert rc == 0

    new_key = _read_master_key_from_env(cfg / ".env")
    assert new_key != original_key
    Fernet(new_key.encode())  # still a valid Fernet key


def test_init_uses_xdg_default_when_no_config_dir(tmp_path):
    """No `--config-dir` → drop into `$HOME/.config/bot-cmder/`. Tests
    autouse-fixture sets HOME to tmp_path/home so this is reproducible."""
    rc = main(["init"])
    assert rc == 0
    expected_cfg = tmp_path / "home" / ".config" / "bot-cmder"
    assert (expected_cfg / "app.yaml").is_file()
    assert (expected_cfg / ".env").is_file()
    # State dir resolution: no `./var/` in CWD, no XDG_STATE_HOME set
    # → ~/.local/state/bot-cmder/
    assert (tmp_path / "home" / ".local" / "state" / "bot-cmder").is_dir()
