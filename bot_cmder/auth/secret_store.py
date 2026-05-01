"""Encrypted SQLite store for per-user TOTP shared secrets.

The store keeps one row per normalized user id (e.g. "telegram:111").
Secrets are encrypted at rest with Fernet using a process-wide master
key supplied via BOT_CMDER_MASTER_KEY. If the master key is lost or
rotated, every user has to re-enroll — there is no key escrow.

Sync SQLite is used deliberately: the access pattern is one read per
OTP submission, which is rare enough that the event loop blocking
cost is negligible compared to the simplicity savings.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from bot_cmder.storage import migrator

# Migrations live next to this module under auth/migrations/. New
# schema changes (added column, new table, new index) get a fresh
# 000N_*.sql file there — never edit the existing ones.
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class SecretStoreError(Exception):
    """Base class for secret-store failures."""


class MasterKeyChanged(SecretStoreError):
    """Stored ciphertext could not be decrypted with the current key.

    Almost always means BOT_CMDER_MASTER_KEY was rotated without
    re-enrolling users.
    """


class SecretStore:
    """Fernet-encrypted SQLite store for TOTP shared secrets."""

    def __init__(self, path: Path | str, master_key: str | bytes) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(master_key, str):
            master_key = master_key.encode("utf-8")
        try:
            self._fernet = Fernet(master_key)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "BOT_CMDER_MASTER_KEY is not a valid Fernet key. "
                "Generate one with: "
                "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
            ) from exc
        self._init_schema()

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        """Open a fresh sqlite3.Connection for this call.

        New connection per call avoids sqlite3's per-thread connection
        affinity (`check_same_thread=True` by default) — handlers run
        on FastAPI worker threads but `__init__` runs on the main
        thread. WAL mode lets concurrent readers in without blocking
        the occasional writer. No application-level lock needed: each
        public method is a single SQL statement, SQLite's own file
        lock plus `INSERT ... ON CONFLICT DO UPDATE` give us all the
        atomicity we want.
        """
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            migrator.apply(conn, _MIGRATIONS_DIR)

    def set_secret(self, user_norm_id: str, secret_b32: str) -> None:
        """Store / overwrite a base32 TOTP secret for a user."""
        encrypted = self._fernet.encrypt(secret_b32.encode("utf-8"))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO totp_secrets (user_norm_id, secret_encrypted) VALUES (?, ?) "
                "ON CONFLICT(user_norm_id) DO UPDATE SET "
                "secret_encrypted = excluded.secret_encrypted, "
                "created_at = CURRENT_TIMESTAMP",
                (user_norm_id, encrypted),
            )

    def get_secret(self, user_norm_id: str) -> str | None:
        """Return the base32 TOTP secret, or None if the user has not enrolled."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT secret_encrypted FROM totp_secrets WHERE user_norm_id = ?",
                (user_norm_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return self._fernet.decrypt(row[0]).decode("utf-8")
        except InvalidToken as exc:
            raise MasterKeyChanged(
                f"cannot decrypt TOTP secret for {user_norm_id!r}; master key likely rotated"
            ) from exc

    def delete_secret(self, user_norm_id: str) -> bool:
        """Remove a user's secret. Returns True if a row was deleted."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM totp_secrets WHERE user_norm_id = ?",
                (user_norm_id,),
            )
            return cursor.rowcount > 0

    def list_users(self) -> list[str]:
        """Return all enrolled user norm_ids, sorted."""
        with self._connect() as conn:
            return [
                row[0] for row in conn.execute("SELECT user_norm_id FROM totp_secrets ORDER BY user_norm_id").fetchall()
            ]
