from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bot_cmder.storage import migrator


def _write(directory: Path, filename: str, sql: str) -> Path:
    p = directory / filename
    p.write_text(sql)
    return p


def _open(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(tmp_path / "test.sqlite")


def test_apply_on_empty_db_runs_every_discovered_migration(tmp_path: Path):
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    _write(mdir, "0001_users.sql", "CREATE TABLE users (id INTEGER PRIMARY KEY);")
    _write(mdir, "0002_index.sql", "CREATE INDEX users_id_idx ON users (id);")

    conn = _open(tmp_path)
    applied = migrator.apply(conn, mdir)

    assert [m.version for m in applied] == [1, 2]
    assert migrator.applied_versions(conn) == [1, 2]
    # The actual table is there
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    assert ("users",) in rows
    assert ("_migrations",) in rows


def test_re_apply_is_a_noop(tmp_path: Path):
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    _write(mdir, "0001_t.sql", "CREATE TABLE t (id INTEGER);")

    conn = _open(tmp_path)
    first = migrator.apply(conn, mdir)
    second = migrator.apply(conn, mdir)
    assert len(first) == 1
    assert second == []


def test_only_new_migrations_apply_on_second_call(tmp_path: Path):
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    _write(mdir, "0001_a.sql", "CREATE TABLE a (id INTEGER);")
    conn = _open(tmp_path)
    migrator.apply(conn, mdir)

    _write(mdir, "0002_b.sql", "CREATE TABLE b (id INTEGER);")
    second = migrator.apply(conn, mdir)
    assert [m.version for m in second] == [2]
    assert migrator.applied_versions(conn) == [1, 2]


def test_duplicate_version_raises(tmp_path: Path):
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    _write(mdir, "0001_a.sql", "CREATE TABLE a (id INTEGER);")
    _write(mdir, "0001_b.sql", "CREATE TABLE b (id INTEGER);")
    conn = _open(tmp_path)
    with pytest.raises(migrator.DuplicateVersion):
        migrator.apply(conn, mdir)


def test_checksum_mismatch_when_applied_migration_was_edited(tmp_path: Path):
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    f = _write(mdir, "0001_t.sql", "CREATE TABLE t (id INTEGER);")
    conn = _open(tmp_path)
    migrator.apply(conn, mdir)

    # Simulate "let me just edit the migration to fix this typo" mistake.
    f.write_text("CREATE TABLE t (id INTEGER, name TEXT);")
    with pytest.raises(migrator.ChecksumMismatch, match="0001_t"):
        migrator.apply(conn, mdir)


def test_bad_sql_rolls_back_within_transaction(tmp_path: Path):
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    _write(mdir, "0001_ok.sql", "CREATE TABLE ok (id INTEGER);")
    _write(
        mdir,
        "0002_bad.sql",
        # First statement creates a table; second is invalid SQL.
        # SQLite supports transactional DDL, so the failure must
        # roll the table creation back too.
        "CREATE TABLE will_be_rolled_back (id INTEGER);\nGIBBERISH;\n",
    )
    conn = _open(tmp_path)
    with pytest.raises(sqlite3.Error):
        migrator.apply(conn, mdir)

    # 0001 applied; 0002 fully rolled back
    assert migrator.applied_versions(conn) == [1]
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='will_be_rolled_back'").fetchall()
    assert rows == []


def test_nonexistent_directory_is_noop(tmp_path: Path):
    conn = _open(tmp_path)
    applied = migrator.apply(conn, tmp_path / "does-not-exist")
    assert applied == []
    # _migrations table is still created
    assert migrator.applied_versions(conn) == []


def test_files_with_other_extensions_are_ignored(tmp_path: Path):
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    _write(mdir, "0001_real.sql", "CREATE TABLE x (id INTEGER);")
    _write(mdir, "README.md", "# notes")
    _write(mdir, "0002_no_ext", "CREATE TABLE y (id INTEGER);")

    applied = migrator.apply(_open(tmp_path), mdir)
    assert [m.version for m in applied] == [1]


def test_discovered_migrations_have_stable_checksums(tmp_path: Path):
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    _write(mdir, "0001_t.sql", "CREATE TABLE t (id INTEGER);\n")
    [m] = migrator.discover(mdir)
    assert m.checksum == m.checksum  # idempotent
    # Same content → same checksum
    other = tmp_path / "other.sql"
    other.write_text("CREATE TABLE t (id INTEGER);\n")
    assert migrator.Migration(version=1, name="t", path=other, sql=other.read_text()).checksum == m.checksum
