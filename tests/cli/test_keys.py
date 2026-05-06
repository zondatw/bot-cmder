"""Tests for `bot-cmder gen-master-key` (issue #20)."""

from __future__ import annotations

from cryptography.fernet import Fernet

from bot_cmder.cli import main


def test_gen_master_key_prints_valid_fernet_key(capsys):
    """Output must be exactly one line of url-safe base64 that
    `Fernet(...)` accepts as a key — `bot-cmder init` and the
    SecretStore bootstrap both depend on this contract."""
    rc = main(["gen-master-key"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    # Single line, no extra noise
    assert "\n" not in out
    # Round-trip through Fernet to confirm validity (correct length +
    # base64 alphabet).
    Fernet(out.encode())


def test_gen_master_key_produces_distinct_outputs(capsys):
    """Two consecutive calls must not produce the same key. Otherwise
    something deterministic crept in (cached RNG, accidental constant)."""
    main(["gen-master-key"])
    a = capsys.readouterr().out.strip()
    main(["gen-master-key"])
    b = capsys.readouterr().out.strip()
    assert a != b
