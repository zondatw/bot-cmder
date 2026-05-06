"""Tests for `bot-cmder serve` (issue #20).

`cmd_serve` shells out to `uvicorn.run(...)`; tests mock that call so
nothing actually binds a port. The interesting contract is the env
var → flag → default precedence, including the deprecation path for
the legacy `BIND_HOST` / `BIND_PORT` / `RELOAD` aliases.
"""

from __future__ import annotations

import warnings
from unittest.mock import patch

from bot_cmder.cli import main
from bot_cmder.cli.serve import _coerce_bool, _coerce_int, _env_or_alias

# --- env / alias resolution helpers ---


def test_env_or_alias_prefers_new_name(monkeypatch):
    monkeypatch.setenv("BOT_CMDER_HOST", "0.0.0.0")
    monkeypatch.setenv("BIND_HOST", "127.0.0.1")
    assert _env_or_alias("BOT_CMDER_HOST", "BIND_HOST") == "0.0.0.0"


def test_env_or_alias_falls_through_to_legacy(monkeypatch):
    monkeypatch.delenv("BOT_CMDER_HOST", raising=False)
    monkeypatch.setenv("BIND_HOST", "127.0.0.1")
    # Reset module-level "warned" set so this test sees the warning
    from bot_cmder.cli import serve as serve_mod

    serve_mod._warned.clear()
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        val = _env_or_alias("BOT_CMDER_HOST", "BIND_HOST")
    assert val == "127.0.0.1"
    deprecations = [w for w in captured if issubclass(w.category, DeprecationWarning)]
    assert deprecations, "expected DeprecationWarning when legacy env var fires"
    assert "BIND_HOST" in str(deprecations[0].message)


def test_env_or_alias_returns_none_when_neither_set(monkeypatch):
    monkeypatch.delenv("BOT_CMDER_HOST", raising=False)
    monkeypatch.delenv("BIND_HOST", raising=False)
    assert _env_or_alias("BOT_CMDER_HOST", "BIND_HOST") is None


def test_coerce_bool_handles_truthy_strings():
    for v in ("1", "true", "TRUE", "yes", "ON"):
        assert _coerce_bool(v, default=False) is True
    for v in ("0", "false", "no", "off", ""):
        assert _coerce_bool(v, default=True) is False


def test_coerce_bool_returns_default_on_none():
    assert _coerce_bool(None, default=True) is True
    assert _coerce_bool(None, default=False) is False


def test_coerce_int_falls_back_on_garbage():
    assert _coerce_int("42", 8000) == 42
    assert _coerce_int(None, 8000) == 8000
    assert _coerce_int("not-a-number", 8000) == 8000


# --- cmd_serve invocation ---


def test_cmd_serve_calls_uvicorn_with_defaults(monkeypatch):
    """No flags + no env vars → uvicorn.run gets 127.0.0.1:47823,
    reload=False. Defaults match the deleted server.py byte-for-byte
    so existing reverse-proxy / tunnel configs keep working."""
    for var in ("BOT_CMDER_HOST", "BOT_CMDER_PORT", "BOT_CMDER_RELOAD", "BIND_HOST", "BIND_PORT", "RELOAD"):
        monkeypatch.delenv(var, raising=False)
    with patch("uvicorn.run") as run_mock:
        rc = main(["serve"])
    assert rc == 0
    run_mock.assert_called_once()
    kwargs = run_mock.call_args.kwargs
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 47823
    assert kwargs["reload"] is False


def test_cmd_serve_cli_flags_win_over_env(monkeypatch):
    """`--host`/`--port` flags beat any env var."""
    monkeypatch.setenv("BOT_CMDER_HOST", "1.2.3.4")
    monkeypatch.setenv("BOT_CMDER_PORT", "9999")
    with patch("uvicorn.run") as run_mock:
        rc = main(["serve", "--host", "127.0.0.1", "--port", "47823"])
    assert rc == 0
    kwargs = run_mock.call_args.kwargs
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 47823


def test_cmd_serve_legacy_env_vars_still_work(monkeypatch):
    """`BIND_HOST` / `BIND_PORT` / `RELOAD` keep working as deprecated
    aliases — emitting warnings but still feeding the right values
    into uvicorn. Critical: this is the migration path; breaking it
    breaks every existing systemd unit pointing at the bot."""
    for var in ("BOT_CMDER_HOST", "BOT_CMDER_PORT", "BOT_CMDER_RELOAD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BIND_HOST", "10.0.0.5")
    monkeypatch.setenv("BIND_PORT", "8888")
    monkeypatch.setenv("RELOAD", "true")
    # Reset warned-set for clean assertion
    from bot_cmder.cli import serve as serve_mod

    serve_mod._warned.clear()
    with patch("uvicorn.run") as run_mock, warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        rc = main(["serve"])
    assert rc == 0
    kwargs = run_mock.call_args.kwargs
    assert kwargs["host"] == "10.0.0.5"
    assert kwargs["port"] == 8888
    assert kwargs["reload"] is True
    # At least one deprecation warning surfaced
    legacy_warnings = [w for w in captured if "BIND_" in str(w.message) or "RELOAD" in str(w.message)]
    assert legacy_warnings, "expected DeprecationWarning when legacy env vars fire"


def test_cmd_serve_reload_flag_overrides_env(monkeypatch):
    """`--reload` works independently of BOT_CMDER_RELOAD env."""
    monkeypatch.delenv("BOT_CMDER_RELOAD", raising=False)
    monkeypatch.delenv("RELOAD", raising=False)
    with patch("uvicorn.run") as run_mock:
        main(["serve", "--reload"])
    assert run_mock.call_args.kwargs["reload"] is True
