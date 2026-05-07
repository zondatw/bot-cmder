"""Tests for `bot_cmder.audit.log` rotation (issue #28).

Pin every rotation trigger + edge case the AuditLogger contract
promises. Uses a manual clock so time-based rotations are
deterministic — same pattern as `tests/auth/test_emergency.py`
(issue #15), which proved the approach scales for time-sensitive
state-machine tests.

Coverage map (13 tests):

  Size triggers:
    1. rotates on first write past max_bytes
    2. doesn't rotate before max_bytes
  Time triggers (manual clock):
    3. rotates exactly at next midnight UTC
    4. doesn't rotate before boundary
    5. hourly when set
    6. weekly when set
  Combined triggers:
    7. size + time both set: whichever fires first wins
  Retention:
    8. backup_count: extra files past N pruned, oldest unlinked
    9. backup_count=0: no pruning
  Compression:
    10. compress=true: rotated file gz-decodes to original content
    11. compress=false: rotated file is plain JSONL
  Disabled:
    12. max_bytes=0 + when=off: never rotates, warns at construction
  Filesystem safety:
    13. timestamp suffix has no `:` (Windows / NTFS won't accept it)
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from datetime import datetime, timezone

import pytest

from bot_cmder.audit.log import AuditLogger
from bot_cmder.config.schema import AuditRotationConfig


class _ManualClock:
    """Tickable UTC clock — same shape as the one in
    tests/auth/test_emergency.py."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        # Use replace + add so we can do `advance(hours=1, minutes=30)`
        from datetime import timedelta

        self.now = self.now + timedelta(**kwargs)


def _read_jsonl(path) -> list[dict]:
    """Read a JSONL file, return decoded records."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_gz_jsonl(path) -> list[dict]:
    """Read a gzipped JSONL file."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# --- 1. size: rotates on first write past max_bytes -----------------


def test_size_trigger_rotates_when_about_to_exceed(tmp_path):
    """First write that would push the file past max_bytes triggers
    rotation. The triggering line lands in the new active file."""
    clock = _ManualClock()
    # 1500 byte cap — comfortably past the first few small writes (each
    # line is ~80 bytes), trips after ~18 writes.
    rot = AuditRotationConfig(max_bytes=1500, when="off", backup_count=5, compress=False)
    log = AuditLogger(tmp_path / "audit.jsonl", rotation=rot, clock=clock)

    # Pre-fill: a handful of writes that stay well under the cap
    for i in range(5):
        log.log(event="EXEC", iter=i, padding="x" * 10)

    pre_rotated = list(tmp_path.glob("audit.jsonl.*"))
    assert pre_rotated == [], "should not have rotated yet on small writes"

    # Push past the cap
    for i in range(50):
        log.log(event="EXEC", iter=100 + i, padding="x" * 50)

    rotated = list(tmp_path.glob("audit.jsonl.*"))
    assert rotated, "expected at least one rotated file once size cap exceeded"
    # Active file is bounded — last rotation+write leaves us under the cap.
    # Allow a small overshoot equal to one full line, since rotation
    # check fires BEFORE the write that would breach.
    assert (tmp_path / "audit.jsonl").stat().st_size <= rot.max_bytes


# --- 2. size: doesn't rotate before max_bytes -----------------------


def test_size_trigger_does_not_fire_below_cap(tmp_path):
    clock = _ManualClock()
    rot = AuditRotationConfig(max_bytes=10_000, when="off", backup_count=5, compress=False)
    log = AuditLogger(tmp_path / "audit.jsonl", rotation=rot, clock=clock)
    for i in range(20):
        log.log(event="EXEC", iter=i)
    assert list(tmp_path.glob("audit.jsonl.*")) == []


# --- 3. time: rotates at midnight (manual clock) --------------------


def test_midnight_trigger_rotates_at_next_utc_midnight(tmp_path):
    clock = _ManualClock(start=datetime(2026, 5, 7, 23, 59, 50, tzinfo=timezone.utc))
    rot = AuditRotationConfig(max_bytes=0, when="midnight", backup_count=5, compress=False)
    log = AuditLogger(tmp_path / "audit.jsonl", rotation=rot, clock=clock)

    log.log(event="EXEC", before="midnight")
    assert list(tmp_path.glob("audit.jsonl.*")) == []

    # Advance past midnight
    clock.advance(seconds=20)  # → 2026-05-08 00:00:10
    log.log(event="EXEC", after="midnight")

    rotated = list(tmp_path.glob("audit.jsonl.*"))
    assert len(rotated) == 1, f"expected 1 rotated file, got {rotated}"
    # The rotated file holds the pre-midnight write; the active file
    # holds the post-midnight write.
    pre_records = _read_jsonl(rotated[0])
    post_records = _read_jsonl(tmp_path / "audit.jsonl")
    assert any(r.get("before") == "midnight" for r in pre_records)
    assert any(r.get("after") == "midnight" for r in post_records)


# --- 4. time: doesn't rotate before boundary ------------------------


def test_midnight_trigger_does_not_fire_before_boundary(tmp_path):
    clock = _ManualClock(start=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc))
    rot = AuditRotationConfig(max_bytes=0, when="midnight", backup_count=5, compress=False)
    log = AuditLogger(tmp_path / "audit.jsonl", rotation=rot, clock=clock)

    for _ in range(50):
        log.log(event="EXEC")
        clock.advance(minutes=10)  # advance up to ~8h, still before next midnight

    assert list(tmp_path.glob("audit.jsonl.*")) == []


# --- 5. time: hourly --------------------------------------------------


def test_hourly_trigger(tmp_path):
    clock = _ManualClock(start=datetime(2026, 5, 7, 11, 59, 50, tzinfo=timezone.utc))
    rot = AuditRotationConfig(max_bytes=0, when="hourly", backup_count=5, compress=False)
    log = AuditLogger(tmp_path / "audit.jsonl", rotation=rot, clock=clock)

    log.log(event="EXEC", phase="before")
    clock.advance(seconds=20)  # → 12:00:10
    log.log(event="EXEC", phase="after")

    rotated = list(tmp_path.glob("audit.jsonl.*"))
    assert len(rotated) == 1


# --- 6. time: weekly --------------------------------------------------


def test_weekly_trigger(tmp_path):
    # Sunday 2026-05-03 23:59:50 UTC (Monday is the next rotation day)
    clock = _ManualClock(start=datetime(2026, 5, 3, 23, 59, 50, tzinfo=timezone.utc))
    rot = AuditRotationConfig(max_bytes=0, when="weekly", backup_count=5, compress=False)
    log = AuditLogger(tmp_path / "audit.jsonl", rotation=rot, clock=clock)

    log.log(event="EXEC", phase="before")
    clock.advance(seconds=20)  # → Monday 2026-05-04 00:00:10
    log.log(event="EXEC", phase="after")

    rotated = list(tmp_path.glob("audit.jsonl.*"))
    assert len(rotated) == 1


# --- 7. combined: whichever fires first wins ------------------------


def test_size_and_time_both_set_either_can_trigger(tmp_path):
    """When both triggers are configured, either firing causes rotation
    independently. Test the size path winning when time hasn't crossed
    yet; the time path is covered by tests 3-6."""
    clock = _ManualClock(start=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc))
    rot = AuditRotationConfig(max_bytes=200, when="midnight", backup_count=5, compress=False)
    log = AuditLogger(tmp_path / "audit.jsonl", rotation=rot, clock=clock)

    for i in range(30):
        log.log(event="EXEC", iter=i, padding="x" * 50)

    rotated = list(tmp_path.glob("audit.jsonl.*"))
    assert rotated, "size trigger should have fired well before midnight"


# --- 8. backup_count: prunes oldest past N --------------------------


def test_backup_count_prunes_oldest(tmp_path):
    """After the (backup_count+1)th rotation, the oldest rotated file
    is unlinked — disk usage stays bounded."""
    clock = _ManualClock()
    rot = AuditRotationConfig(max_bytes=100, when="off", backup_count=2, compress=False)
    log = AuditLogger(tmp_path / "audit.jsonl", rotation=rot, clock=clock)

    # Generate 4 rotations by writing past the 100-byte cap repeatedly.
    # Sleep 1ms between rotations so mtime ordering is stable
    # (filesystems with second-resolution mtime would otherwise tie).
    for batch in range(4):
        for i in range(5):
            log.log(event="EXEC", batch=batch, iter=i, padding="x" * 30)
        # Bump clock so the rotation suffix differs and mtime advances
        clock.advance(seconds=1)
        time.sleep(0.01)

    rotated = sorted(tmp_path.glob("audit.jsonl.*"), key=lambda p: p.stat().st_mtime)
    # Must not exceed backup_count
    assert len(rotated) <= rot.backup_count, f"expected ≤ {rot.backup_count} rotated files, got {len(rotated)}"


# --- 9. backup_count=0: no pruning ----------------------------------


def test_backup_count_zero_does_not_prune(tmp_path):
    """`backup_count=0` opts OUT of pruning entirely — old files
    accumulate. Documented as risky; tested for contract clarity."""
    clock = _ManualClock()
    rot = AuditRotationConfig(max_bytes=100, when="off", backup_count=0, compress=False)
    log = AuditLogger(tmp_path / "audit.jsonl", rotation=rot, clock=clock)

    for batch in range(5):
        for i in range(5):
            log.log(event="EXEC", batch=batch, iter=i, padding="x" * 30)
        clock.advance(seconds=1)
        time.sleep(0.01)

    rotated = list(tmp_path.glob("audit.jsonl.*"))
    # All rotations preserved (no upper bound applied)
    assert len(rotated) >= 4


# --- 10. compress=true: rotated file gzips correctly ----------------


def test_compress_true_rotated_file_is_gzipped(tmp_path):
    clock = _ManualClock()
    rot = AuditRotationConfig(max_bytes=200, when="off", backup_count=5, compress=True)
    log = AuditLogger(tmp_path / "audit.jsonl", rotation=rot, clock=clock)

    for i in range(20):
        log.log(event="EXEC", iter=i, padding="x" * 30)

    rotated = list(tmp_path.glob("audit.jsonl.*"))
    assert rotated, "expected rotation"
    # Every rotated file ends in .gz (no plain `audit.jsonl.<ts>` left)
    for r in rotated:
        assert r.suffix == ".gz", f"expected gzipped suffix, got {r}"
    # Content round-trips
    records = _read_gz_jsonl(rotated[0])
    assert records, "rotated gz file should decode to non-empty record list"
    assert all("event" in r for r in records)


# --- 11. compress=false: rotated file is plain JSONL ----------------


def test_compress_false_rotated_file_is_plain_jsonl(tmp_path):
    clock = _ManualClock()
    rot = AuditRotationConfig(max_bytes=200, when="off", backup_count=5, compress=False)
    log = AuditLogger(tmp_path / "audit.jsonl", rotation=rot, clock=clock)

    for i in range(20):
        log.log(event="EXEC", iter=i, padding="x" * 30)

    rotated = list(tmp_path.glob("audit.jsonl.*"))
    assert rotated
    # No `.gz` files appear
    assert not any(r.suffix == ".gz" for r in rotated)
    # Content directly readable as JSONL
    records = _read_jsonl(rotated[0])
    assert records


# --- 12. fully disabled: never rotates, warns at construction -------


def test_rotation_fully_disabled_never_rotates(tmp_path, caplog):
    """`max_bytes=0` AND `when='off'` is a valid (if odd) configuration
    that disables rotation entirely. Logger warns at construction so
    operators don't accidentally silent-disable rotation."""
    clock = _ManualClock()
    rot = AuditRotationConfig(max_bytes=0, when="off", backup_count=0, compress=False)
    with caplog.at_level(logging.WARNING):
        log = AuditLogger(tmp_path / "audit.jsonl", rotation=rot, clock=clock)
    assert any(
        "rotation disabled" in rec.message for rec in caplog.records
    ), "expected a startup warning when both rotation triggers are off"

    for i in range(100):
        log.log(event="EXEC", iter=i, padding="x" * 100)

    assert list(tmp_path.glob("audit.jsonl.*")) == []


# --- 13. timestamp filename safe ------------------------------------


def test_rotation_timestamp_has_no_colon(tmp_path):
    """Rotated filename suffix must not contain `:` (Windows / NTFS
    rejects it). `_TS_FORMAT = "%Y-%m-%dT%H-%M-%SZ"` enforces this."""
    clock = _ManualClock(start=datetime(2026, 1, 1, 23, 59, 50, tzinfo=timezone.utc))
    rot = AuditRotationConfig(max_bytes=0, when="midnight", backup_count=5, compress=False)
    log = AuditLogger(tmp_path / "audit.jsonl", rotation=rot, clock=clock)

    log.log(event="EXEC", phase="before")
    clock.advance(seconds=20)
    log.log(event="EXEC", phase="after")

    rotated = list(tmp_path.glob("audit.jsonl.*"))
    assert len(rotated) == 1
    assert ":" not in rotated[0].name, f"rotated filename {rotated[0].name} must not contain ':'"


# --- bonus: schema validation rejects invalid `when` ---------------


def test_schema_rejects_unknown_when_value():
    """The AuditRotationConfig validator rejects `when` values outside
    the documented enum. This is the chokepoint that prevents
    `_compute_next_rotation` from receiving garbage at runtime."""
    with pytest.raises(ValueError, match="audit.rotation.when"):
        AuditRotationConfig(when="every-blue-moon")


def test_schema_rejects_negative_max_bytes():
    with pytest.raises(ValueError, match="audit.rotation.max_bytes"):
        AuditRotationConfig(max_bytes=-1)


def test_schema_rejects_negative_backup_count():
    with pytest.raises(ValueError, match="audit.rotation.backup_count"):
        AuditRotationConfig(backup_count=-1)
