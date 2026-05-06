"""Unit tests for `bot_cmder.auth.emergency` (issue #15).

Pin the contract that the dispatcher + /otp builtin rely on:
window lifecycle (grant → active → expired-via-clock-advance),
hard-cap enforcement, manual revoke semantics, and replacement
on re-grant.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot_cmder.auth.emergency import EmergencyWindows


class _ManualClock:
    """Tickable clock for tests so window expiry is deterministic
    instead of needing real `asyncio.sleep`s."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


# --- grant + lifecycle --------------------------------------------------


def test_grant_opens_active_window():
    clock = _ManualClock()
    windows = EmergencyWindows(max_minutes=60, clock=clock)
    window = windows.grant("telegram:1234567890", 15)

    assert window.granted_minutes == 15
    assert window.user_norm_id == "telegram:1234567890"
    assert windows.is_active("telegram:1234567890") is True


def test_no_window_means_not_active():
    """Cold-state read returns False, no entry created."""
    windows = EmergencyWindows()
    assert windows.is_active("telegram:1234567890") is False
    assert windows.get("telegram:1234567890") is None


def test_window_expires_when_clock_passes():
    """After advancing past expires_at, is_active returns False AND
    the entry is lazily evicted from the internal map."""
    clock = _ManualClock()
    windows = EmergencyWindows(max_minutes=60, clock=clock)
    windows.grant("telegram:1234567890", 5)  # 5-min window
    assert windows.is_active("telegram:1234567890")

    clock.advance(minutes=4, seconds=59)
    assert windows.is_active("telegram:1234567890")  # still alive

    clock.advance(seconds=2)  # now 5 min 1 s past grant
    assert windows.is_active("telegram:1234567890") is False
    # Lazily evicted — internal dict no longer has it
    assert "telegram:1234567890" not in windows._windows


def test_remaining_s_counts_down():
    clock = _ManualClock()
    windows = EmergencyWindows(max_minutes=60, clock=clock)
    window = windows.grant("telegram:1234567890", 10)

    assert window.remaining_s(clock()) == 10 * 60

    clock.advance(seconds=30)
    assert window.remaining_s(clock()) == 10 * 60 - 30


# --- hard cap -----------------------------------------------------------


def test_grant_caps_at_max_minutes():
    clock = _ManualClock()
    windows = EmergencyWindows(max_minutes=60, clock=clock)
    # Operator asks for 480 min; should get 60.
    window = windows.grant("telegram:1234567890", 480)
    assert window.granted_minutes == 60


def test_grant_floors_at_one_minute():
    """Zero / negative requested minutes should still produce a
    minimal viable window (1 min) — not crash, not silently make a
    no-op window the user thinks is open."""
    clock = _ManualClock()
    windows = EmergencyWindows(max_minutes=60, clock=clock)
    assert windows.grant("u", 0).granted_minutes == 1
    assert windows.grant("u", -5).granted_minutes == 1


def test_grant_within_cap_passes_through():
    clock = _ManualClock()
    windows = EmergencyWindows(max_minutes=60, clock=clock)
    assert windows.grant("u", 30).granted_minutes == 30


# --- replace + revoke --------------------------------------------------


def test_grant_replaces_existing_window():
    """Re-running /otp emergency 30 while a 5-min window is half-spent
    should open a fresh 30-min window (not extend the old one).
    Operator-explicit action overrides prior state."""
    clock = _ManualClock()
    windows = EmergencyWindows(max_minutes=60, clock=clock)
    first = windows.grant("u", 5)

    clock.advance(minutes=2)  # 3 min still left on first
    second = windows.grant("u", 30)

    assert second.granted_minutes == 30
    # The new expires_at is fully 30 min from the second grant time
    assert second.expires_at == clock() + timedelta(minutes=30)
    # And it's NOT a continuation of the first
    assert second.expires_at != first.expires_at


def test_revoke_returns_window_metadata():
    """Manual /otp end. Returns the revoked window so audit can log
    granted_at / remaining_s before it's gone."""
    clock = _ManualClock()
    windows = EmergencyWindows(max_minutes=60, clock=clock)
    granted = windows.grant("u", 30)

    clock.advance(minutes=10)
    revoked = windows.revoke("u")

    assert revoked is not None
    assert revoked.granted_at == granted.granted_at
    assert revoked.remaining_s(clock()) == 20 * 60
    # Window is gone after revoke
    assert windows.is_active("u") is False


def test_revoke_returns_none_when_no_window():
    """No-op revoke: caller should treat as 'nothing to revoke'."""
    windows = EmergencyWindows()
    assert windows.revoke("u") is None


def test_revoke_returns_none_when_window_already_expired():
    """Calling /otp end after the window already aged out is a no-op,
    not an error — same shape as `no window` so caller can collapse
    both into one response."""
    clock = _ManualClock()
    windows = EmergencyWindows(max_minutes=60, clock=clock)
    windows.grant("u", 5)

    clock.advance(minutes=10)
    assert windows.revoke("u") is None


# --- isolation ---------------------------------------------------------


def test_windows_are_per_norm_id():
    """Granting for telegram:X must not affect slack:Y. Cross-platform
    unification is documented as out-of-scope per issue #15."""
    clock = _ManualClock()
    windows = EmergencyWindows(max_minutes=60, clock=clock)
    windows.grant("telegram:1234567890", 30)

    assert windows.is_active("telegram:1234567890")
    assert windows.is_active("slack:U0123ABCD") is False


def test_max_minutes_property_exposed():
    """`/otp emergency 480` reply needs to mention what the cap was;
    the property is the public surface for that."""
    windows = EmergencyWindows(max_minutes=42)
    assert windows.max_minutes == 42
