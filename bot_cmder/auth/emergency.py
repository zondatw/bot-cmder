"""Emergency OTP-bypass windows (issue #15).

Use case
--------
SRE on-call mid-incident shouldn't lose 5+ seconds per command on
OTP friction (pull phone → read 6 digits → submit → resume). After
proving identity ONCE with a TOTP, this module opens a time-bounded
window during which subsequent PRIVILEGED commands skip the gate
entirely.

Design (per issue #15)
----------------------
- **Activation requires OTP.** Opening the bypass IS itself a
  privileged action; without an OTP requirement we'd have a
  self-bypass loop. Activation goes through the same OTP-stash
  mechanism the existing /otp builtin uses.

- **Per `norm_id`, not per canonical user.** Telegram and Slack
  identities are separate windows. Cross-platform unification is
  documented as out-of-scope and parked for a future hardening
  pass — the SecretStore is also per-norm_id (Phase 4 dogfood
  surfaced the same trade-off).

- **Hard cap on duration.** Operator can request anything; we cap
  at `max_minutes` (default 60). Min 1. Rationale: incident
  longer than 60 min has more cost than a fresh re-auth round.
  Tunable in app.yaml `totp.emergency_max_minutes`.

- **In-memory only.** Restart wipes all windows — fail-safe. Same
  storage class as `PendingOTPSessions`. Multi-instance deployment
  needs re-activation per instance; documented in `docs/otp.md`.

- **No auto-renewal.** Window expires; no sliding extension. If
  the operator needs another window, re-run `/otp emergency N`.

- **Auto-revoke triggers**: time-expired (lazy cleanup on access)
  or explicit `/otp end`. No revoke-on-N-commands or other
  command-count-based limits — the time bound is the safety net.

Audit events
------------
This module raises no events itself — the caller (otp builtin /
dispatcher) owns the audit trail. The module just exposes the
window state. Caller-emitted events are:

  - EMERGENCY_OTP_GRANTED   (activation succeeded)
  - EMERGENCY_OTP_BYPASS    (each PRIVILEGED command run during window)
  - EMERGENCY_OTP_EXPIRED   (window aged out — emitted lazily by
                              caller when get/is_active discovers it)
  - EMERGENCY_OTP_REVOKED   (/otp end manual revoke)
  - EMERGENCY_OTP_INVALID_DURATION (parse error on activation)
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class EmergencyWindow:
    """One active bypass window for a single norm_id.

    Frozen so callers passing it through audit-log kwargs can't
    accidentally mutate state away from what was actually granted.
    """

    user_norm_id: str
    granted_at: datetime
    expires_at: datetime
    granted_minutes: int  # post-cap; what's actually in effect

    def is_active(self, now: datetime) -> bool:
        return now < self.expires_at

    def remaining_s(self, now: datetime) -> int:
        return max(0, int((self.expires_at - now).total_seconds()))


class EmergencyWindows:
    """In-memory map: norm_id → currently-active EmergencyWindow.

    Thread-safe (lock-protected) so concurrent dispatcher tasks can
    check `is_active` without racing the activation handler. Same
    pattern as `PendingOTPSessions`.
    """

    def __init__(
        self,
        *,
        max_minutes: int = 60,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._windows: dict[str, EmergencyWindow] = {}
        self._lock = threading.Lock()
        self._max_minutes = max_minutes
        self._clock = clock

    @property
    def max_minutes(self) -> int:
        """Read-only — exposed for /otp emergency status / activation
        reply text so the operator knows what cap was applied."""
        return self._max_minutes

    def grant(self, user_norm_id: str, requested_minutes: int) -> EmergencyWindow:
        """Open (or replace) a window for `user_norm_id`.

        `requested_minutes` is what the caller asked for; the actual
        granted duration is `max(1, min(requested, max_minutes))` —
        hard cap, no auto-renewal beyond that. Returns the resulting
        window so callers can include `granted_minutes` /
        `expires_at` in their audit events.

        If a window is already active, this REPLACES it. Re-running
        `/otp emergency 30` while you've still got 5 min left of an
        old window opens a fresh 30 min — the operator-explicit
        action overrides the prior state.
        """
        granted = max(1, min(requested_minutes, self._max_minutes))
        now = self._clock()
        window = EmergencyWindow(
            user_norm_id=user_norm_id,
            granted_at=now,
            expires_at=now + timedelta(minutes=granted),
            granted_minutes=granted,
        )
        with self._lock:
            self._windows[user_norm_id] = window
        return window

    def is_active(self, user_norm_id: str) -> bool:
        """True iff there's a non-expired window for `user_norm_id`.

        Lazily evicts expired entries (no background sweeper). The
        dispatcher calls this before every PRIVILEGED command, so
        active windows get refreshed-or-removed on every invocation
        anyway.
        """
        return self.get(user_norm_id) is not None

    def get(self, user_norm_id: str) -> EmergencyWindow | None:
        """Return the active window or None. Same lazy-evict semantics
        as `is_active` — exposed separately so callers that want the
        remaining-time field can avoid a second lookup."""
        with self._lock:
            window = self._windows.get(user_norm_id)
            if window is None:
                return None
            if not window.is_active(self._clock()):
                # Lazy cleanup. Caller (dispatcher / /otp status)
                # sees None and handles it the same as "never had one".
                del self._windows[user_norm_id]
                return None
            return window

    def revoke(self, user_norm_id: str) -> EmergencyWindow | None:
        """Manual revoke (called by /otp end).

        Returns the revoked window so the caller can log
        EMERGENCY_OTP_REVOKED with the granted/remaining metadata,
        or None if there was no active window to revoke.
        """
        with self._lock:
            window = self._windows.pop(user_norm_id, None)
            if window is None:
                return None
            # If it was already expired we treat it as "no window" —
            # /otp end on a dead window is a noop, not an error.
            if not window.is_active(self._clock()):
                return None
            return window
