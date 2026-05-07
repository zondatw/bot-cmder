"""Tests for `bot_cmder.auth.lockout` (issue #33).

Manual clock fixture matching tests/auth/test_emergency.py +
tests/audit/test_log_rotation.py — keeps time-sensitive state-machine
behavior deterministic without sleeps or global datetime mocking.

Coverage map (15 tests + 3 schema validations):

  Threshold + window:
    1. record_failure returns False below threshold, True at trigger
    2. is_locked False before trigger, True after, False after expiry
    3. failures older than failure_window_minutes fall out of count
  Reset semantics:
    4. reset() clears counter even when not yet locked
    5. reset() clears active lockout
  Snapshot + remaining:
    6. remaining_s reaches 0 exactly at expiry
  Per-norm_id isolation:
    7. locking telegram:X doesn't lock slack:Y
  Disabled config:
    8. enabled=False: record_failure no-ops, is_locked False, snapshot empty
  Persistence:
    9. lockout state survives a fresh OTPLockoutState over the same SQLite file
  Already-locked guards:
    10. additional record_failure during active lockout doesn't re-trigger
  Admin override:
    11. admin_unlock clears state even when enabled=False
    12. admin_unlock returns False on no-op (nothing to clear)
  Schema validation:
    13. rejects max_failures < 1
    14. rejects lockout_minutes < 1
    15. rejects failure_window_minutes < 1
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot_cmder.auth.lockout import OTPLockoutState
from bot_cmder.auth.lockout_store import LockoutStore
from bot_cmder.config.schema import OTPLockoutConfig


class _ManualClock:
    """Tickable UTC clock — same shape as the one in
    tests/auth/test_emergency.py and tests/audit/test_log_rotation.py."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


@pytest.fixture
def store(tmp_path: Path) -> LockoutStore:
    return LockoutStore(tmp_path / "lockout.sqlite")


@pytest.fixture
def cfg() -> OTPLockoutConfig:
    return OTPLockoutConfig(
        enabled=True,
        max_failures=5,
        lockout_minutes=10,
        failure_window_minutes=15,
    )


# --- 1. record_failure threshold -----------------------------------


def test_record_failure_returns_false_below_threshold(store, cfg):
    clock = _ManualClock()
    state = OTPLockoutState(store, cfg, clock=clock)
    for i in range(1, cfg.max_failures):
        triggered = state.record_failure("telegram:1")
        assert triggered is False, f"trigger fired prematurely on attempt {i}"


def test_record_failure_triggers_on_max_failures(store, cfg):
    clock = _ManualClock()
    state = OTPLockoutState(store, cfg, clock=clock)
    for _ in range(cfg.max_failures - 1):
        state.record_failure("telegram:1")
    triggered = state.record_failure("telegram:1")
    assert triggered is True
    assert state.is_locked("telegram:1") is True


# --- 2. is_locked before / during / after window ------------------


def test_is_locked_false_before_lockout(store, cfg):
    clock = _ManualClock()
    state = OTPLockoutState(store, cfg, clock=clock)
    assert state.is_locked("telegram:1") is False


def test_is_locked_false_after_expiry(store, cfg):
    """Lazy expiry: is_locked returns False after locked_until passes,
    AND clears the row + failure log so the user starts fresh."""
    clock = _ManualClock()
    state = OTPLockoutState(store, cfg, clock=clock)
    for _ in range(cfg.max_failures):
        state.record_failure("telegram:1")
    assert state.is_locked("telegram:1") is True

    clock.advance(minutes=cfg.lockout_minutes + 1)
    assert state.is_locked("telegram:1") is False


# --- 3. failures older than window fall out -----------------------


def test_old_failures_outside_window_dont_count(store, cfg):
    """Sliding window: a typo at 9am followed by 4 typos at 11am
    (still within 15-min window? no — 9am is 2h ago) shouldn't trigger
    a lockout. The 9am failure is outside the 15-min window, so the
    11am count is 4, not 5."""
    clock = _ManualClock()
    state = OTPLockoutState(store, cfg, clock=clock)

    # First failure — long ago, outside any window
    state.record_failure("telegram:1")

    clock.advance(minutes=cfg.failure_window_minutes + 5)
    # Now 4 fresh failures: total in DB is 5, but only 4 are in-window
    for _ in range(cfg.max_failures - 1):
        state.record_failure("telegram:1")

    assert state.is_locked("telegram:1") is False


# --- 4-5. reset() semantics ----------------------------------------


def test_reset_clears_failures_below_threshold(store, cfg):
    clock = _ManualClock()
    state = OTPLockoutState(store, cfg, clock=clock)
    for _ in range(cfg.max_failures - 1):
        state.record_failure("telegram:1")
    assert state.reset("telegram:1") is True

    # After reset, the failure log is empty — next 4 failures don't
    # trigger lockout
    for _ in range(cfg.max_failures - 1):
        triggered = state.record_failure("telegram:1")
        assert triggered is False


def test_reset_clears_active_lockout(store, cfg):
    clock = _ManualClock()
    state = OTPLockoutState(store, cfg, clock=clock)
    for _ in range(cfg.max_failures):
        state.record_failure("telegram:1")
    assert state.is_locked("telegram:1") is True

    state.reset("telegram:1")
    assert state.is_locked("telegram:1") is False


# --- 6. remaining_s --------------------------------------------------


def test_remaining_s_counts_down_to_zero(store, cfg):
    clock = _ManualClock()
    state = OTPLockoutState(store, cfg, clock=clock)
    for _ in range(cfg.max_failures):
        state.record_failure("telegram:1")

    expected_full = cfg.lockout_minutes * 60
    actual = state.remaining_s("telegram:1")
    # Allow ±1s drift since the trigger clock and snapshot clock
    # are sampled microseconds apart.
    assert abs(actual - expected_full) <= 1

    clock.advance(minutes=cfg.lockout_minutes - 1)
    assert 50 <= state.remaining_s("telegram:1") <= 70

    clock.advance(minutes=2)
    # Past expiry — note remaining_s returns 0, but is_locked also
    # cleans up the row on access.
    assert state.remaining_s("telegram:1") == 0


# --- 7. per-norm_id isolation -------------------------------------


def test_locking_one_norm_id_doesnt_lock_another(store, cfg):
    """Locking telegram:X must not affect slack:Y. This is the core
    contract of the per-norm_id design (see issue #33 maintainer
    answer: scope=per-norm_id)."""
    clock = _ManualClock()
    state = OTPLockoutState(store, cfg, clock=clock)
    for _ in range(cfg.max_failures):
        state.record_failure("telegram:1")
    assert state.is_locked("telegram:1") is True
    assert state.is_locked("slack:U2") is False


# --- 8. disabled config -------------------------------------------


def test_disabled_config_record_failure_no_ops(store):
    cfg = OTPLockoutConfig(enabled=False, max_failures=3, lockout_minutes=10, failure_window_minutes=15)
    clock = _ManualClock()
    state = OTPLockoutState(store, cfg, clock=clock)

    for _ in range(10):
        triggered = state.record_failure("telegram:1")
        assert triggered is False
    assert state.is_locked("telegram:1") is False

    snap = state.snapshot("telegram:1")
    assert snap.locked is False
    assert snap.failure_count == 0


# --- 9. persistence ------------------------------------------------


def test_lockout_persists_across_state_instances(tmp_path, cfg):
    """Drop a lockout, then construct a fresh OTPLockoutState pointed
    at the same SQLite file. The new instance must see the lockout —
    proves SQLite-backed state survives bot restart (the explicit
    design choice from issue #33)."""
    db_path = tmp_path / "lockout.sqlite"

    # Instance 1: trigger lockout, then drop the reference
    clock1 = _ManualClock()
    state1 = OTPLockoutState(LockoutStore(db_path), cfg, clock=clock1)
    for _ in range(cfg.max_failures):
        state1.record_failure("telegram:1")
    assert state1.is_locked("telegram:1") is True
    del state1

    # Instance 2: fresh state machine + store, same DB
    clock2 = _ManualClock(start=clock1.now)  # same clock so we're not past expiry
    state2 = OTPLockoutState(LockoutStore(db_path), cfg, clock=clock2)
    assert state2.is_locked("telegram:1") is True


# --- 10. already-locked guard --------------------------------------


def test_record_failure_during_active_lockout_doesnt_re_trigger(store, cfg):
    """Belt-and-suspenders: dispatcher's pre-flight should reject
    locked /otp before reaching record_failure. But IF a failure
    sneaks through, record_failure must not return True (which would
    fire a duplicate OTP_LOCKOUT_TRIGGERED audit)."""
    clock = _ManualClock()
    state = OTPLockoutState(store, cfg, clock=clock)

    for _ in range(cfg.max_failures):
        state.record_failure("telegram:1")
    assert state.is_locked("telegram:1") is True

    # Clock barely advances — still within the lockout window
    clock.advance(seconds=30)
    triggered_again = state.record_failure("telegram:1")
    assert triggered_again is False


# --- 11-12. admin_unlock semantics --------------------------------


def test_admin_unlock_works_even_when_disabled(store):
    """admin_unlock is a state-cleanup operation, not a policy
    decision. An operator who toggled `enabled: false` mid-incident
    shouldn't have a leftover stale lockout pinning them."""
    cfg_on = OTPLockoutConfig(enabled=True, max_failures=3, lockout_minutes=10, failure_window_minutes=15)
    cfg_off = OTPLockoutConfig(enabled=False, max_failures=3, lockout_minutes=10, failure_window_minutes=15)
    clock = _ManualClock()

    # Trigger lockout under the enabled config
    state = OTPLockoutState(store, cfg_on, clock=clock)
    for _ in range(cfg_on.max_failures):
        state.record_failure("telegram:1")
    assert state.is_locked("telegram:1") is True

    # Operator flips off, then admin_unlocks (e.g. via CLI)
    state_off = OTPLockoutState(store, cfg_off, clock=clock)
    cleared = state_off.admin_unlock("telegram:1")
    assert cleared is True


def test_admin_unlock_returns_false_for_no_op(store, cfg):
    """admin_unlock on a user with no failures + no lockout returns
    False — caller can use this to print a different message."""
    clock = _ManualClock()
    state = OTPLockoutState(store, cfg, clock=clock)
    assert state.admin_unlock("telegram:never-failed") is False


# --- 13-15. schema validation rejections --------------------------


def test_schema_rejects_zero_max_failures():
    with pytest.raises(ValueError, match="max_failures"):
        OTPLockoutConfig(max_failures=0)


def test_schema_rejects_zero_lockout_minutes():
    with pytest.raises(ValueError, match="lockout_minutes"):
        OTPLockoutConfig(lockout_minutes=0)


def test_schema_rejects_zero_failure_window_minutes():
    with pytest.raises(ValueError, match="failure_window_minutes"):
        OTPLockoutConfig(failure_window_minutes=0)


# --- bonus: snapshot for audit-log enrichment ---------------------


def test_snapshot_reflects_current_state(store, cfg):
    """snapshot() is what the dispatcher uses to enrich
    OTP_LOCKOUT_TRIGGERED + OTP_LOCKED_OUT events. Verify it returns
    the right counts + locked flag."""
    clock = _ManualClock()
    state = OTPLockoutState(store, cfg, clock=clock)

    snap0 = state.snapshot("telegram:1")
    assert snap0.locked is False
    assert snap0.failure_count == 0

    for _ in range(2):
        state.record_failure("telegram:1")

    snap1 = state.snapshot("telegram:1")
    assert snap1.locked is False
    assert snap1.failure_count == 2

    # Trip the lockout
    for _ in range(cfg.max_failures - 2):
        state.record_failure("telegram:1")

    snap2 = state.snapshot("telegram:1")
    assert snap2.locked is True
    assert snap2.failure_count >= cfg.max_failures
