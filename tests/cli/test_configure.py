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

import httpx
import pytest
import respx

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
            False,  # in-flow validate against API? No (skip — C7 default)
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
            False,  # in-flow validate? No
        ]
    )

    rc = main(["configure", "telegram", "--config-dir", str(cfg)])
    assert rc == 0

    # 3 calls: mode select + bot token + validate-confirm.
    # If webhook-secret prompt had fired, we'd see a 4th confirm() too.
    assert len(fake.calls) == 3
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

    fake_questionary(["polling", "111:fake-fake-fake-fake-fake-fake-fake-xx", False])

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

    fake_questionary(["polling", "111:dry-run-token-which-is-long-enough-now", False])

    rc = main(["configure", "telegram", "--config-dir", str(cfg), "--dry-run"])
    assert rc == 0

    # File untouched
    assert env_path.read_text(encoding="utf-8") == before

    out = capsys.readouterr().out
    # Diff output contains the proposed line
    assert "TELEGRAM_TOKEN=111:dry-run-token-which-is-long-enough-now" in out


# --- C5: Discord flow -------------------------------------------------


def test_discord_interactions_flow_writes_all_four_keys(tmp_path, fake_questionary):
    """Walk interactions mode: mode → token → app_id → public_key →
    skip guild_id. Expect MODE + BOT_TOKEN + APPLICATION_ID +
    PUBLIC_KEY in the .env."""
    cfg = tmp_path / "cfg"
    env_path = _seed_env(cfg)

    fake_questionary(
        [
            "interactions",  # mode
            "fake-bot-token-opaque-discord-string",  # bot token
            "123456789012345678",  # 18-digit app ID
            "a" * 64,  # 64-char hex public key
            "",  # guild ID (blank = skipped)
            False,  # validate? No
        ]
    )

    rc = main(["configure", "discord", "--config-dir", str(cfg)])
    assert rc == 0

    written = env_path.read_text(encoding="utf-8")
    assert "DISCORD_MODE=interactions" in written
    assert "DISCORD_BOT_TOKEN=fake-bot-token-opaque-discord-string" in written
    assert "DISCORD_APPLICATION_ID=123456789012345678" in written
    assert f"DISCORD_PUBLIC_KEY={'a' * 64}" in written
    # Guild ID was skipped (blank), no DISCORD_GUILD_ID line written
    assert "DISCORD_GUILD_ID" not in written


def test_discord_gateway_flow_skips_public_key(tmp_path, fake_questionary):
    """Gateway mode doesn't sign-check — public key prompt should NOT
    fire. Verify by checking call count vs interactions mode."""
    cfg = tmp_path / "cfg"
    env_path = _seed_env(cfg)

    fake = fake_questionary(
        [
            "gateway",  # mode
            "fake-gateway-token-opaque",  # bot token
            "999888777666555444",  # app ID
            "",  # guild ID skipped
            False,  # validate? No
        ]
    )

    rc = main(["configure", "discord", "--config-dir", str(cfg)])
    assert rc == 0

    # 5 calls — mode select, bot_token password, app_id text, guild_id text,
    # validate confirm. NO public_key prompt fired.
    assert len(fake.calls) == 5

    written = env_path.read_text(encoding="utf-8")
    assert "DISCORD_MODE=gateway" in written
    assert "DISCORD_PUBLIC_KEY" not in written


def test_discord_with_guild_id_writes_it(tmp_path, fake_questionary):
    """Operator who fills in the guild ID gets it written. Validator
    enforces the snowflake format."""
    cfg = tmp_path / "cfg"
    env_path = _seed_env(cfg)

    fake_questionary(
        [
            "gateway",
            "fake-token",
            "111111111111111111",  # app ID
            "222222222222222222",  # guild ID — passes regex
            False,  # validate? No
        ]
    )

    rc = main(["configure", "discord", "--config-dir", str(cfg)])
    assert rc == 0

    assert "DISCORD_GUILD_ID=222222222222222222" in env_path.read_text(encoding="utf-8")


# --- C6: Slack flow ---------------------------------------------------


def test_slack_events_flow_writes_bot_token_signing_secret_url(tmp_path, fake_questionary):
    """Events mode requires bot_token + signing_secret. URL is optional."""
    cfg = tmp_path / "cfg"
    env_path = _seed_env(cfg)

    fake_questionary(
        [
            "events",  # mode
            "xoxb-fake-bot-token-for-testing",  # bot token (xoxb- prefix)
            "0123456789abcdef0123456789abcdef",  # 32-hex signing secret
            "my-tunnel.ngrok-free.dev",  # request URL
            False,  # validate? No
        ]
    )

    rc = main(["configure", "slack", "--config-dir", str(cfg)])
    assert rc == 0

    written = env_path.read_text(encoding="utf-8")
    assert "SLACK_MODE=events" in written
    assert "SLACK_BOT_TOKEN=xoxb-fake-bot-token-for-testing" in written
    assert "SLACK_SIGNING_SECRET=0123456789abcdef0123456789abcdef" in written
    assert "SLACK_REQUEST_URL=my-tunnel.ngrok-free.dev" in written
    # NOT a socket-mode field
    assert "SLACK_APP_TOKEN" not in written


def test_slack_socket_flow_writes_app_token_skips_request_url(tmp_path, fake_questionary):
    """Socket mode needs app_token instead of request_url. Signing
    secret is optional — operator may skip with blank."""
    cfg = tmp_path / "cfg"
    env_path = _seed_env(cfg)

    fake = fake_questionary(
        [
            "socket",
            "xoxb-fake-socket-bot-token",
            "xapp-1-FAKE-APP-TOKEN-VALUE",  # xapp- prefix
            "",  # skip signing secret
            False,  # validate? No
        ]
    )

    rc = main(["configure", "slack", "--config-dir", str(cfg)])
    assert rc == 0

    # Mode + bot_token + app_token + signing_secret + validate = 5 calls
    assert len(fake.calls) == 5

    written = env_path.read_text(encoding="utf-8")
    assert "SLACK_MODE=socket" in written
    assert "SLACK_APP_TOKEN=xapp-1-FAKE-APP-TOKEN-VALUE" in written
    # Socket mode doesn't ask for request URL
    assert "SLACK_REQUEST_URL" not in written


def test_slack_socket_signing_secret_optional_skip(tmp_path, fake_questionary):
    """Empty input for the optional signing_secret in socket mode
    should leave the key unset (or empty), NOT trigger validator
    failure."""
    cfg = tmp_path / "cfg"
    env_path = _seed_env(cfg)

    fake_questionary(["socket", "xoxb-x", "xapp-y", "", False])

    rc = main(["configure", "slack", "--config-dir", str(cfg)])
    assert rc == 0

    written = env_path.read_text(encoding="utf-8")
    # Signing secret line either absent or empty — validator allows blank
    assert "SLACK_SIGNING_SECRET=0123" not in written


# --- C7: In-flow live validation --------------------------------------
#
# Tests use respx to mock httpx — exact same pattern as
# tests/adapters/discord/test_client.py. Validator-fail-then-save-
# anyway tests the [r]etry / [s]ave-anyway / [a]bort menu's "save"
# branch; validator-fail-abort tests the "abort" branch (changes
# discarded, file untouched).


@respx.mock
def test_telegram_validate_success_writes(tmp_path, fake_questionary):
    """Operator opts in to validation, Telegram getMe returns ok=True
    → writes proceed normally."""
    cfg = tmp_path / "cfg"
    env_path = _seed_env(cfg)

    fake_questionary(
        [
            "polling",
            "111:fake-token-which-is-long-enough-to-pass",
            True,  # validate? Yes
        ]
    )
    respx.get("https://api.telegram.org/bot111:fake-token-which-is-long-enough-to-pass/getMe").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"username": "fakebot"}})
    )

    rc = main(["configure", "telegram", "--config-dir", str(cfg)])
    assert rc == 0
    assert "TELEGRAM_TOKEN=111:fake-token-which-is-long-enough-to-pass" in env_path.read_text(encoding="utf-8")


@respx.mock
def test_telegram_validate_fail_abort_discards_changes(tmp_path, fake_questionary):
    """Validation fails; operator picks Abort → file is NOT written."""
    cfg = tmp_path / "cfg"
    env_path = _seed_env(cfg)
    before = env_path.read_text(encoding="utf-8")

    fake_questionary(
        [
            "polling",
            "111:bad-token-format-but-passes-regex-yyy",
            True,  # validate? Yes
            "abort",  # validation-failed menu choice
        ]
    )
    respx.get("https://api.telegram.org/bot111:bad-token-format-but-passes-regex-yyy/getMe").mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )

    rc = main(["configure", "telegram", "--config-dir", str(cfg)])
    assert rc == 0
    # File untouched — abort caused flow to return False, dispatcher
    # saw "no changes to write"
    assert env_path.read_text(encoding="utf-8") == before


@respx.mock
def test_telegram_validate_fail_save_anyway_writes(tmp_path, fake_questionary):
    """Validation fails; operator picks Save anyway → write proceeds."""
    cfg = tmp_path / "cfg"
    env_path = _seed_env(cfg)

    fake_questionary(
        [
            "polling",
            "111:operator-knows-this-token-is-fine-anyway",
            True,  # validate? Yes
            "save",  # validation-failed menu choice
        ]
    )
    respx.get("https://api.telegram.org/bot111:operator-knows-this-token-is-fine-anyway/getMe").mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )

    rc = main(["configure", "telegram", "--config-dir", str(cfg)])
    assert rc == 0
    assert "TELEGRAM_TOKEN=111:operator-knows-this-token-is-fine-anyway" in env_path.read_text(encoding="utf-8")


@respx.mock
def test_discord_validate_success(tmp_path, fake_questionary):
    """Discord users/@me returns bot=True → validate passes."""
    cfg = tmp_path / "cfg"
    env_path = _seed_env(cfg)

    fake_questionary(
        [
            "gateway",
            "fake-discord-bot-token",
            "111111111111111111",
            "",  # skip guild_id
            True,  # validate? Yes
        ]
    )
    respx.get("https://discord.com/api/v10/users/@me").mock(
        return_value=httpx.Response(200, json={"bot": True, "username": "fakebot#0001"})
    )

    rc = main(["configure", "discord", "--config-dir", str(cfg)])
    assert rc == 0
    assert "DISCORD_BOT_TOKEN=fake-discord-bot-token" in env_path.read_text(encoding="utf-8")


@respx.mock
def test_slack_validate_success(tmp_path, fake_questionary):
    """Slack auth.test returns ok=True → validate passes."""
    cfg = tmp_path / "cfg"
    env_path = _seed_env(cfg)

    fake_questionary(
        [
            "socket",
            "xoxb-fake-bot",
            "xapp-1-FAKE-APP-TOKEN",
            "",  # skip signing_secret
            True,  # validate? Yes
        ]
    )
    respx.post("https://slack.com/api/auth.test").mock(
        return_value=httpx.Response(200, json={"ok": True, "team": "fake-team", "user": "fakebot"})
    )

    rc = main(["configure", "slack", "--config-dir", str(cfg)])
    assert rc == 0
    assert "SLACK_BOT_TOKEN=xoxb-fake-bot" in env_path.read_text(encoding="utf-8")
