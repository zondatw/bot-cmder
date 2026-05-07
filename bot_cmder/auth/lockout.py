"""OTP brute-force lockout state machine (issue #33).

Per-norm_id failure counter + lockout window. Companion to the
TOTP gate in `bot_cmder/auth/totp.py`:

    /otp <code> arrives
        ↓
    OTPLockoutState.is_locked(norm_id)?
        yes → reject (audit: OTP_LOCKED_OUT), pending session NOT consumed
        no  → fall through to existing OTP verify path
                ↓
            verify pass → OTPLockoutState.reset(norm_id)
                            (no-op if no failures recorded)
            verify fail → OTPLockoutState.record_failure(norm_id)
                          → returns True if this attempt triggered the lockout

State persists across bot restarts via SQLite (see `lockout_store.py`).
This survives a "restart the bot to clear my failures" attack — and
also makes the `bot-cmder unlock-totp` admin CLI work without any
IPC to the running bot process: both touch the same SQLite file.

Concurrency: `record_failure()` and `is_locked()` are designed to be
called from the dispatcher's `/otp` handler, which already serializes
through the registry's per-command lock. The store layer uses
SQLite's own file lock for cross-process safety.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_cmder.auth.lockout_store import LockoutStore
    from bot_cmder.config.schema import OTPLockoutConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LockoutSnapshot:
    """Read-only view of a norm_id's current state.

    Returned by `OTPLockoutState.snapshot()` for audit logging — caller
    pulls fields like `remaining_s` + `failure_count` to enrich
    OTP_LOCKOUT_TRIGGERED / OTP_LOCKED_OUT events without reaching
    into the store directly.
    """

    locked: bool
    locked_until: datetime | None
    failure_count: int

    def remaining_s(self, now: datetime) -> int:
        if self.locked_until is None:
            return 0
        return max(0, int((self.locked_until - now).total_seconds()))


class OTPLockoutState:
    """Per-norm_id lockout tracker, SQLite-backed.

    Constructed once at app startup, threaded through to
    `bot_cmder.commands.builtin.otp` alongside the existing
    `PendingOTPSessions` + `TOTPVerifier`. Disabled-by-config (per
    `config.totp.lockout.enabled = False`) means every method
    no-ops — `record_failure` returns False, `is_locked` returns
    False, etc.
    """

    def __init__(
        self,
        store: LockoutStore,
        config: OTPLockoutConfig,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._store = store
        self._config = config
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def is_locked(self, norm_id: str) -> bool:
        """True iff `norm_id` is currently within an active lockout
        window. Prunes expired lockouts on access (lazy cleanup —
        avoids needing a background task)."""
        if not self._config.enabled:
            return False
        now = self._clock()
        row = self._store.get_lockout(norm_id)
        if row is None:
            return False
        if now >= row.locked_until:
            # Expired — remove + reset failure log so a one-shot
            # incident doesn't leave the next attempt with a stale
            # near-threshold counter. Audit event for expiry is
            # logged by the dispatcher on the next /otp attempt; the
            # state-machine layer just does cleanup.
            self._store.clear_lockout(norm_id)
            self._store.clear_failures(norm_id)
            return False
        return True

    def remaining_s(self, norm_id: str) -> int:
        """Seconds left in the current lockout, 0 if not locked."""
        if not self._config.enabled:
            return 0
        row = self._store.get_lockout(norm_id)
        if row is None:
            return 0
        return max(0, int((row.locked_until - self._clock()).total_seconds()))

    def snapshot(self, norm_id: str) -> LockoutSnapshot:
        """Read-only summary for audit-log enrichment."""
        if not self._config.enabled:
            return LockoutSnapshot(locked=False, locked_until=None, failure_count=0)
        row = self._store.get_lockout(norm_id)
        failures = self._store.count_failures_since(
            norm_id, self._clock() - timedelta(minutes=self._config.failure_window_minutes)
        )
        return LockoutSnapshot(
            locked=row is not None and self._clock() < row.locked_until,
            locked_until=row.locked_until if row is not None else None,
            failure_count=failures,
        )

    def record_failure(self, norm_id: str) -> bool:
        """Log an OTP_INVALID for `norm_id`. Returns True iff this
        attempt crossed the failure threshold and triggered a fresh
        lockout — caller can use this to fire a one-shot
        OTP_LOCKOUT_TRIGGERED audit event.

        Already-locked norm_ids never trigger again here (the
        dispatcher's pre-flight check rejects before reaching this
        path). Defensive: if we somehow record a failure during an
        active lockout, the second-trigger condition is suppressed.
        """
        if not self._config.enabled:
            return False
        now = self._clock()
        self._store.add_failure(norm_id, now)

        # Already locked? Don't double-trigger.
        existing = self._store.get_lockout(norm_id)
        if existing is not None and now < existing.locked_until:
            return False

        # Count failures within the sliding window.
        window_start = now - timedelta(minutes=self._config.failure_window_minutes)
        count = self._store.count_failures_since(norm_id, window_start)

        if count < self._config.max_failures:
            return False

        # Threshold crossed — open a lockout window.
        locked_until = now + timedelta(minutes=self._config.lockout_minutes)
        self._store.set_lockout(
            norm_id=norm_id,
            locked_at=now,
            locked_until=locked_until,
            failure_count=count,
        )
        logger.info(
            "OTP lockout triggered for %s (%d failures within %d-min window, locked until %s)",
            norm_id,
            count,
            self._config.failure_window_minutes,
            locked_until.isoformat(timespec="seconds"),
        )
        return True

    def reset(self, norm_id: str) -> bool:
        """Clear the failure log + any active lockout for `norm_id`.
        Called on successful OTP. Returns True iff anything was
        actually cleared (used in tests; the dispatcher doesn't
        care about the return value).
        """
        if not self._config.enabled:
            return False
        cleared_failures = self._store.clear_failures(norm_id)
        cleared_lockout = self._store.clear_lockout(norm_id)
        return cleared_failures > 0 or cleared_lockout

    def admin_unlock(self, norm_id: str) -> bool:
        """Force-clear a lockout via admin override (`bot-cmder
        unlock-totp`). Same effect as `reset()` — split out as its
        own method so callers can audit `OTP_LOCKOUT_ADMIN_RESET`
        with confidence the unlock was intentional, not just a
        side effect of a successful OTP."""
        # Even when disabled, admin_unlock works — it's a state-
        # cleanup operation, not a policy decision. An operator who
        # toggled `enabled: false` mid-incident shouldn't have a
        # leftover stale lockout pinning them.
        cleared_failures = self._store.clear_failures(norm_id)
        cleared_lockout = self._store.clear_lockout(norm_id)
        return cleared_failures > 0 or cleared_lockout
