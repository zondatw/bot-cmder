from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot_cmder.auth.pending import PendingOTPSessions
from bot_cmder.core.events import Platform


def test_stash_and_pop_returns_session():
    p = PendingOTPSessions(ttl_s=60)
    s = p.stash(
        user_norm_id="u",
        chat_id="42",
        platform=Platform.TELEGRAM,
        command_name="restart",
        args=["api"],
    )
    popped = p.pop("u")
    assert popped is s
    assert p.pop("u") is None


def test_args_are_copied_so_mutating_the_input_doesnt_leak():
    p = PendingOTPSessions()
    args = ["api"]
    s = p.stash(
        user_norm_id="u",
        chat_id="42",
        platform=Platform.TELEGRAM,
        command_name="restart",
        args=args,
    )
    args.append("evil")
    assert s.args == ["api"]


def test_overwrite_keeps_only_latest():
    p = PendingOTPSessions()
    p.stash(user_norm_id="u", chat_id="1", platform=Platform.TELEGRAM, command_name="a", args=[])
    p.stash(user_norm_id="u", chat_id="1", platform=Platform.TELEGRAM, command_name="b", args=[])
    s = p.pop("u")
    assert s.command_name == "b"


def test_is_expired_uses_injected_clock():
    now = [datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)]
    p = PendingOTPSessions(ttl_s=10, clock=lambda: now[0])
    s = p.stash(user_norm_id="u", chat_id="c", platform=Platform.TELEGRAM, command_name="x", args=[])
    assert p.is_expired(s) is False
    now[0] += timedelta(seconds=11)
    assert p.is_expired(s) is True
