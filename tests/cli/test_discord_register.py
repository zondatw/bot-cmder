"""Precedence tests for `bot_cmder.cli.discord_register._resolve_guild`.

The subcommand supports four ways to choose a registration scope; the
order matters because a forgotten `DISCORD_GUILD_ID` in `.env` could
otherwise silently scope a prod push to the dev guild. This test
nails the precedence so the contract documented in the docstring
stays honest.

Issue #20 moved this from `scripts/register_discord_commands.py` to
the unified CLI as `bot-cmder discord-register`; the schema-building
helpers (`_build_command_schema` etc.) and `_resolve_guild` carried
over verbatim, so this test file mostly tracks the import path move.
"""

from __future__ import annotations

import argparse

import pytest

from bot_cmder.cli import main
from bot_cmder.cli.discord_register import _build_command_schema, _resolve_guild
from bot_cmder.config.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """Make `get_settings()` ignore the repo's on-disk `.env`.

    Without this, a real value in `.env` (e.g. the dev guild ID) bleeds
    into tests and `monkeypatch.delenv` can't overcome it — pydantic-
    settings re-reads `.env` after env vars. Bypass by stubbing
    `get_settings` with one that constructs `Settings(_env_file=None)`,
    so only os.environ (which monkeypatch CAN control) feeds the value.
    """
    get_settings.cache_clear()
    monkeypatch.setattr(
        "bot_cmder.cli.discord_register.get_settings",
        lambda: Settings(_env_file=None),
    )
    yield
    get_settings.cache_clear()


def _ns(*, guild: str | None = None, global_: bool = False) -> argparse.Namespace:
    return argparse.Namespace(guild=guild, global_=global_)


def test_global_flag_wins_over_env(monkeypatch):
    monkeypatch.setenv("DISCORD_GUILD_ID", "111")
    assert _resolve_guild(_ns(global_=True)) is None


def test_guild_flag_wins_over_env(monkeypatch):
    monkeypatch.setenv("DISCORD_GUILD_ID", "111")
    assert _resolve_guild(_ns(guild="222")) == "222"


def test_env_used_when_no_flag(monkeypatch):
    monkeypatch.setenv("DISCORD_GUILD_ID", "111")
    assert _resolve_guild(_ns()) == "111"


def test_no_env_no_flag_means_global(monkeypatch):
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    assert _resolve_guild(_ns()) is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_env_treated_as_unset(monkeypatch, blank):
    # `DISCORD_GUILD_ID=` (a placeholder line in .env.example) must
    # not produce a push to a guild called "" — that's a 404 trap.
    monkeypatch.setenv("DISCORD_GUILD_ID", blank)
    assert _resolve_guild(_ns()) is None


def test_guild_and_global_are_mutually_exclusive():
    # argparse should reject `--guild=X --global` because they're in
    # the same mutually_exclusive_group. Through the unified CLI
    # dispatcher (`bot-cmder discord-register --guild=X --global`).
    with pytest.raises(SystemExit):
        main(["discord-register", "--guild", "111", "--global"])


# --- Issue #18 — /otp gets the SUB_COMMAND treatment ----------------


def test_otp_schema_uses_sub_commands():
    """`/otp` must NOT be a flat-STRING command. Instead it has four
    SUB_COMMAND children (code / emergency / end / status) so Discord
    autocomplete renders each variant with its own option schema —
    crucial because `/otp end` and `/otp status` take no args, and a
    flat required-STRING option made them literally unreachable from
    the Discord client UI before this fix."""
    schema = _build_command_schema("otp", "Submit OTP code, or control emergency-bypass window")

    assert schema["name"] == "otp"
    assert schema["type"] == 1  # CHAT_INPUT

    sub_names = [opt["name"] for opt in schema["options"]]
    assert sub_names == ["code", "emergency", "end", "status"]

    by_name = {opt["name"]: opt for opt in schema["options"]}
    # All four are SUB_COMMAND (option type 1, NOT STRING type 3).
    for name, sub in by_name.items():
        assert sub["type"] == 1, f"/otp {name} must be a SUB_COMMAND"

    # `code` carries one required STRING leaf.
    code_opts = by_name["code"]["options"]
    assert len(code_opts) == 1
    assert code_opts[0]["type"] == 3  # STRING
    assert code_opts[0]["required"] is True

    # `emergency` carries one required INTEGER leaf — Discord will
    # validate this client-side, so users can't submit "five" by
    # mistake (Telegram still parses ints from text in the dispatcher).
    em_opts = by_name["emergency"]["options"]
    assert len(em_opts) == 1
    assert em_opts[0]["type"] == 4  # INTEGER
    assert em_opts[0]["required"] is True

    # `end` and `status` take no inner options.
    assert "options" not in by_name["end"]
    assert "options" not in by_name["status"]


def test_help_still_no_args():
    """Sanity check the unrelated `/help` path is untouched."""
    schema = _build_command_schema("help", "Show command list")
    assert "options" not in schema


def test_default_command_still_uses_flat_args_string():
    """Anything not /otp / not in _NO_ARGS_COMMANDS keeps the legacy
    one-STRING-option shape (e.g. /service, /kubectl, /ssh)."""
    schema = _build_command_schema("service", "Run a service action")
    assert len(schema["options"]) == 1
    assert schema["options"][0]["name"] == "args"
    assert schema["options"][0]["type"] == 3
    assert schema["options"][0]["required"] is False
