from __future__ import annotations

from pathlib import Path

import pyotp
import pytest
from cryptography.fernet import Fernet

from bot_cmder.auth.secret_store import SecretStore
from bot_cmder.auth.totp import TOTPVerifier


@pytest.fixture
def store(tmp_path: Path) -> SecretStore:
    return SecretStore(tmp_path / "t.sqlite", Fernet.generate_key().decode())


def test_enroll_returns_secret_and_provisioning_uri(store: SecretStore):
    v = TOTPVerifier(store)
    secret, uri = v.enroll("telegram:1")
    assert secret  # base32 string
    assert uri.startswith("otpauth://totp/")
    assert "secret=" in uri
    assert v.is_enrolled("telegram:1")


def test_verify_accepts_current_code(store: SecretStore):
    v = TOTPVerifier(store)
    secret, _ = v.enroll("u")
    code = pyotp.TOTP(secret).now()
    assert v.verify("u", code) is True


def test_verify_rejects_replay_within_window(store: SecretStore):
    v = TOTPVerifier(store)
    secret, _ = v.enroll("u")
    code = pyotp.TOTP(secret).now()
    assert v.verify("u", code) is True
    assert v.verify("u", code) is False, "replay must be rejected"


def test_verify_rejects_unenrolled_user(store: SecretStore):
    v = TOTPVerifier(store)
    assert v.verify("nobody:0", "123456") is False


@pytest.mark.parametrize("bad", ["", "abc", "12345", "1234567", "abcdef"])
def test_verify_rejects_malformed_code(store: SecretStore, bad: str):
    v = TOTPVerifier(store)
    v.enroll("u")
    assert v.verify("u", bad) is False


def test_verify_rejects_unrelated_code(store: SecretStore):
    v = TOTPVerifier(store)
    v.enroll("u")
    # Pick a code essentially guaranteed to be wrong (000000 has odds 1/1e6).
    assert v.verify("u", "000000") in (False, True)
    # The above is non-deterministic by design; instead test with a code
    # generated from a *different* secret (vanishingly unlikely collision).
    assert v.verify("u", pyotp.TOTP(pyotp.random_base32()).now()) is False


def test_is_enrolled_false_when_not_enrolled(store: SecretStore):
    v = TOTPVerifier(store)
    assert v.is_enrolled("nobody") is False
