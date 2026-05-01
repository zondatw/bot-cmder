from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from bot_cmder.cli import main
from bot_cmder.config.settings import get_settings


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch):
    """Point Settings + AppConfig at a tmp dir so the CLI doesn't touch real state."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BOT_CMDER_MASTER_KEY", key)
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "missing.yaml"))
    monkeypatch.chdir(tmp_path)  # var/ paths land here
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_enroll_then_list_then_revoke(capsys):
    rc = main(["enroll-totp", "--user", "telegram:1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Enrolled: telegram:1" in out
    assert "otpauth://totp/" in out
    assert "secret=" in out

    rc = main(["list-totp"])
    assert rc == 0
    assert "telegram:1" in capsys.readouterr().out

    rc = main(["revoke-totp", "--user", "telegram:1"])
    assert rc == 0
    assert "Revoked" in capsys.readouterr().out

    rc = main(["list-totp"])
    assert rc == 0
    assert "No users enrolled" in capsys.readouterr().out


def test_revoke_unknown_user_returns_nonzero(capsys):
    rc = main(["revoke-totp", "--user", "nobody:0"])
    assert rc == 1


def test_missing_master_key_errors(monkeypatch, capsys):
    monkeypatch.delenv("BOT_CMDER_MASTER_KEY", raising=False)
    get_settings.cache_clear()
    rc = main(["enroll-totp", "--user", "telegram:1"])
    assert rc == 1
    assert "BOT_CMDER_MASTER_KEY" in capsys.readouterr().err
