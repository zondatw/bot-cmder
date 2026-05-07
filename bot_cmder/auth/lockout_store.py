"""SQLite-backed storage for OTP lockout state (issue #33).

Lives next to `secret_store.py` and shares the same migration
infrastructure (`bot_cmder.storage.migrator`). Two tables:

  - `otp_failures` — append-only failure log keyed by (norm_id, ts).
    Read by `count_failures_since()` for sliding-window threshold
    checks. Cleared on successful OTP / lockout expiry / admin unlock.

  - `otp_lockouts` — exactly one row per locked norm_id (PRIMARY KEY).
    Set by `set_lockout()` when the threshold is crossed; cleared
    by `clear_lockout()` on expiry / success / admin unlock.

Why SQLite (vs in-memory) per issue #33 design decision:
  - State survives bot restart — restarting the bot does NOT clear
    an attacker's failure count
  - `bot-cmder unlock-totp` admin CLI works without IPC: both the
    running bot and the CLI just talk to the same SQLite file
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bot_cmder.storage import migrator

# Migrations live next to this module under auth/migrations/, shared
# with secret_store.py — both stores' schemas are versioned in the
# same forward-only sequence (0001_initial.sql for TOTP secrets,
# 0002_lockout.sql for lockout tables).
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


@dataclass(frozen=True)
class LockoutRow:
    """Read-only view of an `otp_lockouts` row."""

    user_norm_id: str
    locked_at: datetime
    locked_until: datetime
    failure_count: int


def _to_iso(t: datetime) -> str:
    return t.isoformat()


def _from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


class LockoutStore:
    """SQLite-backed persistence for OTPLockoutState.

    Public surface is intentionally narrow — every method is a single
    SQL statement (sometimes within a tiny `with conn:` transaction).
    The state-machine logic (threshold check, lockout calculation)
    lives in `OTPLockoutState`; this class is purely "save + load".
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        """Fresh connection per call — same pattern as SecretStore.
        WAL mode so reader/writer concurrency is non-blocking."""
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            migrator.apply(conn, _MIGRATIONS_DIR)

    # --- failures table ----------------------------------------------

    def add_failure(self, norm_id: str, at: datetime) -> None:
        """Append a single OTP_INVALID record. Schema uses rowid PK
        so duplicate timestamps coexist — manual-clock tests
        produce them, and production wouldn't suppress a rapid retry
        against the user's interest anyway."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO otp_failures (user_norm_id, failed_at) VALUES (?, ?)",
                (norm_id, _to_iso(at)),
            )

    def count_failures_since(self, norm_id: str, since: datetime) -> int:
        """Count failures for `norm_id` with `failed_at >= since`.
        Used for the sliding-window threshold check."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM otp_failures WHERE user_norm_id = ? AND failed_at >= ?",
                (norm_id, _to_iso(since)),
            ).fetchone()
        return int(row[0]) if row else 0

    def clear_failures(self, norm_id: str) -> int:
        """Delete all failure rows for `norm_id`. Returns the count
        deleted (used by tests; production callers ignore)."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM otp_failures WHERE user_norm_id = ?", (norm_id,))
            return cur.rowcount

    # --- lockouts table ----------------------------------------------

    def get_lockout(self, norm_id: str) -> LockoutRow | None:
        """Fetch the active lockout row for `norm_id`, or None.

        Does NOT filter by expiry — caller compares `locked_until`
        against the current clock. Lazy expiry semantics live in
        OTPLockoutState (one place to coordinate timezone-aware
        comparison)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_norm_id, locked_at, locked_until, failure_count "
                "FROM otp_lockouts WHERE user_norm_id = ?",
                (norm_id,),
            ).fetchone()
        if row is None:
            return None
        return LockoutRow(
            user_norm_id=row[0],
            locked_at=_from_iso(row[1]),
            locked_until=_from_iso(row[2]),
            failure_count=int(row[3]),
        )

    def set_lockout(
        self,
        *,
        norm_id: str,
        locked_at: datetime,
        locked_until: datetime,
        failure_count: int,
    ) -> None:
        """UPSERT a lockout row. Re-locking an already-locked norm_id
        (which the state machine guards against, but defensive here
        too) replaces the prior row's `locked_until` — locks don't
        accumulate."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO otp_lockouts (user_norm_id, locked_at, locked_until, failure_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_norm_id) DO UPDATE SET
                    locked_at = excluded.locked_at,
                    locked_until = excluded.locked_until,
                    failure_count = excluded.failure_count
                """,
                (norm_id, _to_iso(locked_at), _to_iso(locked_until), failure_count),
            )

    def clear_lockout(self, norm_id: str) -> bool:
        """Delete the lockout row for `norm_id`. Returns True iff a
        row was deleted."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM otp_lockouts WHERE user_norm_id = ?", (norm_id,))
            return cur.rowcount > 0
