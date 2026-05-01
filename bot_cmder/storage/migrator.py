"""Tiny forward-only SQLite migration runner.

Why we have this and not Alembic / Yoyo:

  - bot-cmder owns small, narrowly-scoped tables (TOTP secrets now;
    SSH inventory cache, scheduled-task state, etc. coming). The
    full SQLAlchemy migration stack would be heavier than the code
    it migrates.
  - Plain .sql files in a directory are the easiest thing for an
    SRE-shaped audience to read in a code review.
  - The migrator pins applied content with a sha256 checksum, so a
    later "let me just edit migration 3 to fix this typo" mistake
    fails loud instead of silently diverging environments.

Conventions enforced by this module:

  - Migration files live under <store-module>/migrations/
  - Filename matches `^(\\d{4})_(\\w+)\\.sql$` — the 4-digit prefix
    is the canonical version, the snake_case suffix is human-readable
    description.
  - Migrations are forward-only. There is no `down`. To "undo" a
    migration, write a new forward migration that compensates.
  - Each migration runs in its own transaction; the bookkeeping
    insert into _migrations happens in the same transaction so a
    failed migration leaves no half-applied state.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_FILENAME_RE = re.compile(r"^(\d{4})_(\w+)\.sql$")


class MigrationError(Exception):
    """Base class for migration failures."""


class DuplicateVersion(MigrationError):
    """Two files claim the same NNNN_ prefix."""


class ChecksumMismatch(MigrationError):
    """An already-applied migration's file content has been edited."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


def discover(directory: Path) -> list[Migration]:
    """List migrations in `directory` sorted ascending by version."""
    if not directory.exists():
        return []
    found: dict[int, Migration] = {}
    for entry in sorted(directory.iterdir()):
        m = _FILENAME_RE.match(entry.name)
        if m is None:
            continue
        version = int(m.group(1))
        name = m.group(2)
        if version in found:
            raise DuplicateVersion(
                f"two migrations share version {version:04d}: " f"{found[version].path.name} and {entry.name}"
            )
        found[version] = Migration(
            version=version,
            name=name,
            path=entry,
            sql=entry.read_text(encoding="utf-8"),
        )
    return sorted(found.values(), key=lambda m: m.version)


def _split_statements(sql: str) -> list[str]:
    """Split a multi-statement SQL script on top-level `;` boundaries.

    Strips full-line `--` comments and ignores blank lines. Doesn't
    try to be smart about strings containing `;` — keep migration SQL
    sane (one statement per `;`) and this is enough.
    """
    out: list[str] = []
    current: list[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        current.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(current).rstrip(";").strip()
            if stmt:
                out.append(stmt)
            current = []
    tail = "\n".join(current).rstrip(";").strip()
    if tail:
        out.append(tail)
    return out


def apply(conn: sqlite3.Connection, directory: Path) -> list[Migration]:
    """Apply every pending migration. Returns the migrations that ran.

    Idempotent: a second call with no new files is a no-op.

    Each migration's statements run inside an explicit BEGIN/COMMIT
    together with the bookkeeping insert into `_migrations`. We use
    explicit transaction control instead of `with conn:` because the
    sqlite3 stdlib's autocommit-by-default + `executescript()` quirks
    make implicit transaction wrapping unreliable for DDL — which is
    most of what migrations do. SQLite itself supports transactional
    DDL, so an exception mid-migration rolls back cleanly.

    Raises `ChecksumMismatch` if an already-applied migration's
    content has changed since application.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT    NOT NULL,
            applied_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            checksum   TEXT    NOT NULL
        )
        """
    )
    applied: dict[int, str] = {row[0]: row[1] for row in conn.execute("SELECT version, checksum FROM _migrations")}

    discovered = discover(directory)
    newly_applied: list[Migration] = []
    for migration in discovered:
        prior_checksum = applied.get(migration.version)
        if prior_checksum is not None:
            if prior_checksum != migration.checksum:
                raise ChecksumMismatch(
                    f"migration {migration.label} content has changed since it was applied; "
                    "edit a new migration file instead of modifying an old one"
                )
            continue

        try:
            conn.execute("BEGIN")
            for stmt in _split_statements(migration.sql):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO _migrations (version, name, checksum) VALUES (?, ?, ?)",
                (migration.version, migration.name, migration.checksum),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        newly_applied.append(migration)
    return newly_applied


def applied_versions(conn: sqlite3.Connection) -> list[int]:
    """Read applied version numbers, sorted. Empty list if `_migrations` not yet present."""
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='_migrations'").fetchone()
    if row is None:
        return []
    return [r[0] for r in conn.execute("SELECT version FROM _migrations ORDER BY version")]
