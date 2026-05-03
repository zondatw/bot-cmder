"""Precedence tests for `scripts.register_discord_commands._resolve_guild`.

The script supports four ways to choose a registration scope; the
order matters because a forgotten `DISCORD_GUILD_ID` in `.env` could
otherwise silently scope a prod push to the dev guild. This test
nails the precedence so the contract documented in the docstring
stays honest.
"""

from __future__ import annotations

import argparse

import pytest

from bot_cmder.config.settings import Settings, get_settings
from scripts.register_discord_commands import _resolve_guild, main


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
        "scripts.register_discord_commands.get_settings",
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
    # the same mutually_exclusive_group.
    with pytest.raises(SystemExit):
        main(["--guild", "111", "--global"])
