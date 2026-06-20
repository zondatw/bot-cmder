"""Smoke tests for the `bot-cmder` CLI dispatcher (issue #20).

Pin the help-listing contract + --version handling. Subcommand-
specific behavior lives in their own test files (test_init.py,
test_serve.py, test_keys.py).
"""

from __future__ import annotations

import pytest

from bot_cmder.cli import main


def test_help_lists_all_subcommands(capsys):
    """`bot-cmder --help` should advertise every subcommand the
    dispatcher wires up. Failing this means a subcommand registration
    silently dropped — easy to do when refactoring add_subparsers calls."""
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for cmd in (
        "serve",
        "init",
        "configure",
        "gen-master-key",
        "enroll-totp",
        "list-totp",
        "revoke-totp",
        "unlock-totp",
        "discord-register",
        "slack-manifest",
    ):
        assert cmd in out, f"`{cmd}` missing from --help output"


def test_version_flag_prints_package_version(capsys):
    """`bot-cmder --version` should print exactly `bot-cmder X.Y.Z`
    where X.Y.Z is the package's `__version__`. Source-of-truth check
    so a missed bump in `bot_cmder/__init__.py` doesn't ship a stale
    version string in the CLI."""
    from bot_cmder import __version__

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.strip() == f"bot-cmder {__version__}"


def test_no_args_exits_nonzero(capsys):
    """No subcommand → argparse prints usage + exits 2 (the standard
    argparse contract for `required=True` subparsers). Don't accept 0
    here — that would mean we silently no-op'd."""
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_unknown_subcommand_exits_nonzero():
    """Typo'd subcommand → argparse error + exit 2."""
    with pytest.raises(SystemExit) as exc:
        main(["definitely-not-a-real-subcommand"])
    assert exc.value.code == 2
