"""Tests for `bot_cmder.cli._envfile` (issue #45).

The parser/writer is the load-bearing component for `bot-cmder
configure`: get it wrong and operators' .env files lose comments,
master keys, or other adapters' values. These tests pin the
contract before any wizard code consumes it.

Coverage:
  Round-trip / order / comment preservation
    1. load(empty file) → empty EnvFile
    2. load + save round-trips byte-identical for a typical .env
    3. comments and blank lines preserved verbatim
    4. malformed lines preserved as opaque raw lines
    5. duplicate keys: last wins for get(); save preserves both
       lines (operator might have wanted the override)
    6. outer matching quotes stripped from parsed value (single + double)
    7. inner quotes preserved
  set() semantics
    8. set existing key updates in place — line index unchanged,
       comments around it untouched
    9. set new key appends EOF with sentinel header
   10. set multiple new keys → only ONE header (not three)
   11. set value=None → KEY= (empty assignment, line stays)
  Atomic write
   12. chmod 0o600 forced on save
   13. simulated rename failure leaves original file intact +
       temp file cleaned up
   14. parent directory auto-created
  Diff
   15. diff against identical state → empty string
   16. diff after set() shows the change in unified-diff format
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bot_cmder.cli._envfile import EnvFile

# --- 1-2. load / round-trip ----------------------------------------


def test_load_missing_file_returns_empty(tmp_path):
    env = EnvFile.load(tmp_path / "no-such.env")
    assert env.path == tmp_path / "no-such.env"
    assert env.lines == []
    assert env.keys() == []


def test_round_trip_byte_identical(tmp_path):
    """Load + immediate save without mutation produces a byte-for-byte
    identical file. This is THE contract — any other behavior would
    silently mutate operators' .env on every wizard run."""
    src = tmp_path / ".env"
    original = (
        "# Header comment\n"
        "\n"
        "BOT_CMDER_MASTER_KEY=abc123\n"
        "\n"
        "# Telegram\n"
        "TELEGRAM_TOKEN=123:fakefakefake\n"
        "# TELEGRAM_WEBHOOK_SECRET=optional\n"
    )
    src.write_text(original, encoding="utf-8")

    env = EnvFile.load(src)
    env.save()

    assert src.read_text(encoding="utf-8") == original


# --- 3. comments + blanks preserved -------------------------------


def test_comments_and_blanks_preserved(tmp_path):
    src = tmp_path / ".env"
    src.write_text("# c1\n\n  # indented comment\nKEY=value\n\n# trailing\n", encoding="utf-8")
    env = EnvFile.load(src)
    # 6 lines including the blanks
    assert len(env.lines) == 6
    # Only one is a real key
    assert env.keys() == ["KEY"]
    env.save()
    assert src.read_text(encoding="utf-8") == "# c1\n\n  # indented comment\nKEY=value\n\n# trailing\n"


# --- 4. malformed lines preserved ---------------------------------


def test_malformed_lines_preserved_as_opaque(tmp_path):
    """Lines that don't match KEY=value or # comment (e.g. shell
    assignments with lowercase keys, weird syntax) survive verbatim
    so we never silently delete operator intent."""
    src = tmp_path / ".env"
    src.write_text("not a key\nkey=lowercase\nKEY=value\n", encoding="utf-8")
    env = EnvFile.load(src)
    # Only the strict-match line is parsed as a key
    assert env.keys() == ["KEY"]
    env.save()
    assert src.read_text(encoding="utf-8") == "not a key\nkey=lowercase\nKEY=value\n"


# --- 5. duplicate keys --------------------------------------------


def test_duplicate_keys_last_wins_for_get_but_both_preserved(tmp_path):
    src = tmp_path / ".env"
    src.write_text("FOO=first\nBAR=other\nFOO=second\n", encoding="utf-8")
    env = EnvFile.load(src)
    assert env.get("FOO") == "second"
    assert env.keys() == ["FOO", "BAR", "FOO"]
    env.save()
    # Both FOO lines round-trip
    assert src.read_text(encoding="utf-8") == "FOO=first\nBAR=other\nFOO=second\n"


# --- 6-7. quoting -------------------------------------------------


@pytest.mark.parametrize(
    "raw_value,expected",
    [
        ('"quoted"', "quoted"),
        ("'single'", "single"),
        ("nope", "nope"),
        ("\"mixed'", "\"mixed'"),  # unmatched quotes → preserved
        ('"with spaces"', "with spaces"),
        ('"a\'b"', "a'b"),  # inner quote preserved
    ],
)
def test_quote_stripping(tmp_path, raw_value, expected):
    src = tmp_path / ".env"
    src.write_text(f"K={raw_value}\n", encoding="utf-8")
    env = EnvFile.load(src)
    assert env.get("K") == expected


# --- 8-11. set() semantics ----------------------------------------


def test_set_existing_key_updates_in_place(tmp_path):
    """The line index of the key doesn't change — comments above/below
    it stay positionally adjacent. Critical for the operator's mental
    map of `.env` sections."""
    src = tmp_path / ".env"
    src.write_text(
        "# Top\n"
        "BOT_CMDER_MASTER_KEY=key1\n"
        "\n"
        "# Telegram section\n"
        "TELEGRAM_TOKEN=old\n"
        "TELEGRAM_WEBHOOK_SECRET=ws\n",
        encoding="utf-8",
    )
    env = EnvFile.load(src)
    env.set("TELEGRAM_TOKEN", "new-value-xxxxxxxxxxxxxxxxxxx")
    env.save()
    out = src.read_text(encoding="utf-8")
    # Order preserved: master key first, blank, telegram comment, then
    # the updated TOKEN, then WEBHOOK_SECRET
    assert out == (
        "# Top\n"
        "BOT_CMDER_MASTER_KEY=key1\n"
        "\n"
        "# Telegram section\n"
        "TELEGRAM_TOKEN=new-value-xxxxxxxxxxxxxxxxxxx\n"
        "TELEGRAM_WEBHOOK_SECRET=ws\n"
    )


def test_set_new_key_appends_under_sentinel_header(tmp_path):
    src = tmp_path / ".env"
    src.write_text("EXISTING=value\n", encoding="utf-8")
    env = EnvFile.load(src)
    env.set("BRAND_NEW", "hello")
    env.save()
    out = src.read_text(encoding="utf-8")
    assert "added by `bot-cmder configure`" in out
    assert "BRAND_NEW=hello\n" in out
    # Header appears exactly once
    assert out.count("added by `bot-cmder configure`") == 1


def test_set_multiple_new_keys_share_one_header(tmp_path):
    """The sentinel header gets printed once per file, not per new
    key — otherwise three new platforms in one wizard run would
    yield three identical headers."""
    src = tmp_path / ".env"
    src.write_text("EXISTING=value\n", encoding="utf-8")
    env = EnvFile.load(src)
    env.set("NEW_A", "a")
    env.set("NEW_B", "b")
    env.set("NEW_C", "c")
    env.save()
    out = src.read_text(encoding="utf-8")
    assert out.count("added by `bot-cmder configure`") == 1
    # All three new keys land in order, AFTER the existing key
    pre_marker, post_marker = out.split("added by `bot-cmder configure`", 1)
    assert "EXISTING=value" in pre_marker
    assert "NEW_A=a" in post_marker
    assert "NEW_B=b" in post_marker
    assert "NEW_C=c" in post_marker


def test_set_value_none_clears_to_empty_assignment(tmp_path):
    """Operator picked [Clear] — keep the KEY= line so the
    placeholder comment above it still has context. Don't delete
    the row entirely (that would orphan the comment)."""
    src = tmp_path / ".env"
    src.write_text(
        "# Telegram bot token (from BotFather)\nTELEGRAM_TOKEN=old\n",
        encoding="utf-8",
    )
    env = EnvFile.load(src)
    env.set("TELEGRAM_TOKEN", None)
    env.save()
    out = src.read_text(encoding="utf-8")
    assert out == "# Telegram bot token (from BotFather)\nTELEGRAM_TOKEN=\n"
    # Re-loading the empty assignment returns "" (set), not None (absent)
    env2 = EnvFile.load(src)
    assert env2.get("TELEGRAM_TOKEN") == ""


# --- 12-14. atomic write + perms + parent-dir creation -----------


def test_save_chmods_600(tmp_path):
    """Same hard contract as `bot-cmder init` — the .env carries the
    Fernet master key, so world-readability is a leak surface."""
    env = EnvFile(path=tmp_path / ".env")
    env.set("KEY", "value")
    env.save()
    mode = (tmp_path / ".env").stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got 0o{oct(mode)[2:]}"


def test_save_atomic_rolls_back_on_replace_failure(tmp_path):
    """If `os.replace` raises mid-write, the temp file must be cleaned
    up AND the original `.env` (if any) must be unchanged."""
    src = tmp_path / ".env"
    original = "ORIGINAL=value\n"
    src.write_text(original, encoding="utf-8")

    env = EnvFile.load(src)
    env.set("KEY", "new")

    with (
        patch("bot_cmder.cli._envfile.os.replace", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        env.save()

    # Original file unchanged
    assert src.read_text(encoding="utf-8") == original
    # No temp file leaked
    leftovers = list(tmp_path.glob(".env.*.tmp"))
    assert leftovers == [], f"temp file leaked: {leftovers}"


def test_save_creates_parent_directory(tmp_path):
    """If the parent dir doesn't exist yet (e.g. first wizard run
    against a config-dir that init forgot), create it. Matches
    init_cmd's mkdir-then-write pattern."""
    target = tmp_path / "fresh-cfg" / ".env"
    assert not target.parent.exists()
    env = EnvFile(path=target)
    env.set("KEY", "value")
    env.save()
    assert target.is_file()
    assert (target.read_text(encoding="utf-8")) == "\n# --- added by `bot-cmder configure` ---\nKEY=value\n"


# --- 15-16. diff ---------------------------------------------------


def test_diff_empty_when_no_changes(tmp_path):
    src = tmp_path / ".env"
    src.write_text("KEY=value\n", encoding="utf-8")
    env = EnvFile.load(src)
    original = EnvFile.load(src)
    assert env.diff(original) == ""


def test_diff_unified_format_after_set(tmp_path):
    src = tmp_path / ".env"
    src.write_text("KEY=value\n", encoding="utf-8")
    original = EnvFile.load(src)

    env = EnvFile.load(src)
    env.set("KEY", "different")
    diff = env.diff(original)

    assert "-KEY=value" in diff
    assert "+KEY=different" in diff
    # Standard unified-diff fileheader format
    assert "(current)" in diff
    assert "(proposed)" in diff
