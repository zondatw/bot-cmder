from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from bot_cmder.auth.secret_store import MasterKeyChanged, SecretStore


def _key() -> str:
    return Fernet.generate_key().decode()


def test_set_and_get_secret_roundtrips(tmp_path: Path):
    store = SecretStore(tmp_path / "totp.sqlite", _key())
    assert store.get_secret("telegram:1") is None
    store.set_secret("telegram:1", "JBSWY3DPEHPK3PXP")
    assert store.get_secret("telegram:1") == "JBSWY3DPEHPK3PXP"


def test_set_replaces_existing(tmp_path: Path):
    store = SecretStore(tmp_path / "t.sqlite", _key())
    store.set_secret("u", "AAAAAAAAAAAAAAAA")
    store.set_secret("u", "BBBBBBBBBBBBBBBB")
    assert store.get_secret("u") == "BBBBBBBBBBBBBBBB"


def test_list_users_sorted(tmp_path: Path):
    store = SecretStore(tmp_path / "t.sqlite", _key())
    store.set_secret("c", "X")
    store.set_secret("a", "Y")
    store.set_secret("b", "Z")
    assert store.list_users() == ["a", "b", "c"]


def test_delete_returns_true_when_existed(tmp_path: Path):
    store = SecretStore(tmp_path / "t.sqlite", _key())
    store.set_secret("u", "X")
    assert store.delete_secret("u") is True
    assert store.delete_secret("u") is False
    assert store.get_secret("u") is None


def test_invalid_master_key_raises_at_construction(tmp_path: Path):
    with pytest.raises(ValueError, match="Fernet key"):
        SecretStore(tmp_path / "t.sqlite", "this-is-not-a-fernet-key")


def test_master_key_rotation_raises_master_key_changed(tmp_path: Path):
    path = tmp_path / "t.sqlite"
    SecretStore(path, _key()).set_secret("u", "S")
    with pytest.raises(MasterKeyChanged):
        SecretStore(path, _key()).get_secret("u")


def test_creates_parent_directory(tmp_path: Path):
    nested = tmp_path / "deep" / "var" / "totp.sqlite"
    SecretStore(nested, _key()).set_secret("u", "S")
    assert nested.exists()
