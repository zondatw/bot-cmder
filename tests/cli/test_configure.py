"""Tests for `bot_cmder.cli.configure` (issue #45).

Skeleton-level + Telegram-flow coverage. Discord and Slack flows
(C5 / C6) and live API validation (C7) follow the same patterns
introduced here.

UI mocking strategy: instead of driving real `prompt_toolkit` via
`create_pipe_input` keystroke streams, we replace the `questionary`
module in `sys.modules` with a tiny fake whose select/password/
text/confirm widgets just return pre-canned answers from a queue.
This tests OUR logic (which mode is asked when, which env keys
get written, which validators fire) without coupling to
prompt_toolkit's internals — questionary's own tests cover the
widget rendering.
"""

from __future__ import annotations

import sys
import types
from collections import deque
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


def test_direct_mode_non_interactive_refuses(tmp_path, capsys):
    """`--non-interactive` + a platform that actually wants to prompt
    (post-C4: Telegram does) exits 1. Direct mode reaches the flow's
    `_can_prompt` check, which fails with the same shape as menu mode."""
    cfg = tmp_path / "cfg"
    _seed_env(cfg)
    rc = main(["configure", "telegram", "--config-dir", str(cfg), "--non-interactive"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "non-interactive" in err


# --- Telegram flow tests (C4) -----------------------------------------
#
# Strategy: replace `questionary` in sys.modules with a fake that
# returns answers from a queue. This tests our orchestration logic
# (which key is asked when, validator wiring, conditional fields)
# without coupling to prompt_toolkit's internals.


class _FakeAnswer:
    """Stand-in for `questionary.Question`. Just remembers the answer
    so `.unsafe_ask()` can return it."""

    def __init__(self, answer):
        self._answer = answer

    def unsafe_ask(self):
        return self._answer


class _FakeQuestionary(types.ModuleType):
    """sys.modules['questionary'] swap-in. Pops answers from `answers`
    queue in call order. Tests assert order is correct by carefully
    sequencing the answer list."""

    def __init__(self, answers: list):
        super().__init__("questionary")
        self._answers = deque(answers)
        self.calls: list[tuple[str, tuple, dict]] = []
        # questionary exposes Choice + Separator as classes; just
        # forward them as no-op shims so configure.py's construction
        # of `questionary.Choice(...)` keeps working.
        self.Choice = lambda *a, **k: types.SimpleNamespace(args=a, kwargs=k)
        self.Separator = lambda *a, **k: types.SimpleNamespace(args=a, kwargs=k)

    def _next(self, kind: str, args, kwargs):
        if not self._answers:
            raise AssertionError(f"_FakeQuestionary: no answer queued for {kind}() call (args={args!r})")
        self.calls.append((kind, args, kwargs))
        return _FakeAnswer(self._answers.popleft())

    def select(self, *args, **kwargs):
        return self._next("select", args, kwargs)

    def password(self, *args, **kwargs):
        return self._next("password", args, kwargs)

    def text(self, *args, **kwargs):
        return self._next("text", args, kwargs)

    def confirm(self, *args, **kwargs):
        return self._next("confirm", args, kwargs)


@pytest.fixture
def fake_questionary(monkeypatch):
    """Install a fake `questionary` module for the duration of the
    test. Returns a function that, given an answer list, swaps it
    into sys.modules and returns the fake (so the test can inspect
    `.calls` afterwards)."""

    def _install(answers: list) -> _FakeQuestionary:
        fake = _FakeQuestionary(answers)
        monkeypatch.setitem(sys.modules, "questionary", fake)
        return fake

    return _install


# --- C4: Telegram flow ------------------------------------------------


def test_telegram_webhook_flow_writes_token_and_generated_secret(tmp_path, fake_questionary, capsys):
    """Walk through webhook mode. Mode select returns 'webhook',
    bot token replaces the empty .env value, webhook secret is
    auto-generated. Expect TELEGRAM_MODE + TELEGRAM_TOKEN +
    TELEGRAM_WEBHOOK_SECRET in the written file."""
    cfg = tmp_path / "cfg"
    env_path = _seed_env(cfg)

    fake_questionary(
        [
            "webhook",  # mode select
            "123456789:fake-but-correct-format-token-XXXXXXX",  # bot token prompt
            True,  # confirm auto-generate webhook secret? Yes
        ]
    )

    rc = main(["configure", "telegram", "--config-dir", str(cfg)])
    assert rc == 0

    written = env_path.read_text(encoding="utf-8")
    assert "TELEGRAM_MODE=webhook" in written
    assert "TELEGRAM_TOKEN=123456789:fake-but-correct-format-token-XXXXXXX" in written
    assert "TELEGRAM_WEBHOOK_SECRET=" in written
    # Generated secret has some entropy — token_urlsafe(32) is ~43 chars
    assert len(written.split("TELEGRAM_WEBHOOK_SECRET=", 1)[1].splitlines()[0]) >= 20


def test_telegram_polling_flow_skips_webhook_secret(tmp_path, fake_questionary, capsys):
    """Polling mode shouldn't even ask about webhook secret. The
    fake's answer queue having only 2 entries (mode + token) proves
    no third prompt fired."""
    cfg = tmp_path / "cfg"
    env_path = _seed_env(cfg)

    fake = fake_questionary(
        [
            "polling",
            "987654321:another-fake-format-token-YYYYYYYY",
        ]
    )

    rc = main(["configure", "telegram", "--config-dir", str(cfg)])
    assert rc == 0

    # Only TWO questionary calls fired (mode select + bot token).
    # If webhook-secret prompt had fired, we'd see a confirm() too.
    assert len(fake.calls) == 2
    assert fake.calls[0][0] == "select"  # mode
    assert fake.calls[1][0] == "password"  # bot token

    written = env_path.read_text(encoding="utf-8")
    assert "TELEGRAM_MODE=polling" in written
    assert "TELEGRAM_TOKEN=987654321:another-fake-format-token-YYYYYYYY" in written
    assert "TELEGRAM_WEBHOOK_SECRET" not in written or "WEBHOOK_SECRET=\n" in written


def test_telegram_flow_preserves_other_platforms_values(tmp_path, fake_questionary):
    """Configuring Telegram MUST NOT touch existing Discord / Slack
    values. This is the surgical-update contract — operators trust the
    wizard not to wipe out adapters it isn't touching."""
    cfg = tmp_path / "cfg"
    env_path = cfg / ".env"
    cfg.mkdir(parents=True)
    env_path.write_text(
        "BOT_CMDER_MASTER_KEY=fake-master\n"
        "DISCORD_BOT_TOKEN=preserved-discord\n"
        "SLACK_BOT_TOKEN=xoxb-preserved-slack\n",
        encoding="utf-8",
    )

    fake_questionary(["polling", "111:fake-fake-fake-fake-fake-fake-fake-xx"])

    rc = main(["configure", "telegram", "--config-dir", str(cfg)])
    assert rc == 0

    written = env_path.read_text(encoding="utf-8")
    # Master key untouched
    assert "BOT_CMDER_MASTER_KEY=fake-master" in written
    # Other platforms BYTE-FOR-BYTE preserved
    assert "DISCORD_BOT_TOKEN=preserved-discord" in written
    assert "SLACK_BOT_TOKEN=xoxb-preserved-slack" in written


def test_telegram_existing_value_keep_writes_nothing(tmp_path, fake_questionary):
    """When TELEGRAM_TOKEN is already set and operator picks 'Keep',
    no write happens. Tests the Keep/Replace/Clear path of
    _apply_field."""
    cfg = tmp_path / "cfg"
    env_path = cfg / ".env"
    cfg.mkdir(parents=True)
    env_path.write_text(
        "BOT_CMDER_MASTER_KEY=fake\n" "TELEGRAM_MODE=polling\n" "TELEGRAM_TOKEN=existing-token-value-here\n",
        encoding="utf-8",
    )
    original = env_path.read_text(encoding="utf-8")

    from bot_cmder.cli.configure import FieldChoice

    fake_questionary(
        [
            "polling",  # mode select — same as current, no change
            FieldChoice.KEEP,  # Keep existing token
        ]
    )

    rc = main(["configure", "telegram", "--config-dir", str(cfg), "--dry-run"])
    assert rc == 0
    assert env_path.read_text(encoding="utf-8") == original


def test_telegram_dry_run_writes_nothing_but_shows_diff(tmp_path, fake_questionary, capsys):
    """`--dry-run` prints the diff but doesn't touch the file. md5
    before == after."""
    cfg = tmp_path / "cfg"
    env_path = _seed_env(cfg)
    before = env_path.read_text(encoding="utf-8")

    fake_questionary(["polling", "111:dry-run-token-which-is-long-enough-now"])

    rc = main(["configure", "telegram", "--config-dir", str(cfg), "--dry-run"])
    assert rc == 0

    # File untouched
    assert env_path.read_text(encoding="utf-8") == before

    out = capsys.readouterr().out
    # Diff output contains the proposed line
    assert "TELEGRAM_TOKEN=111:dry-run-token-which-is-long-enough-now" in out
