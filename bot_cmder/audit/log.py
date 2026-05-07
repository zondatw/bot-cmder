"""Append-only JSONL audit log with built-in rotation (issue #28).

Single point of truth for "what happened, when, by whom, with what
outcome" across the entire bot. Every command attempt, ACL deny, OTP
event, SSH connection, emergency-bypass grant/use/revoke writes one
JSONL line here.

Rotation policy (configured in app.yaml under `audit.rotation`):

  - Size cap: rotate when active file would exceed `max_bytes`.
  - Time partition: rotate at "midnight" / "hourly" / "daily" /
    "weekly" boundary (UTC), or "off" to disable time-based.
  - Whichever trigger fires first wins.

Rotated files renamed to `audit.jsonl.<UTC-timestamp>` (filesystem-safe
suffix avoiding `:`). Optionally gzipped. Old rotations past
`backup_count` get unlinked at rotation time so disk usage stays
bounded.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot_cmder.config.schema import AuditRotationConfig

logger = logging.getLogger(__name__)

# Suffix format for rotated files. ISO 8601 with `:` swapped for `-`
# because Windows / NTFS won't tolerate `:` in filenames and we want
# the same suffix shape on every host.
_TS_FORMAT = "%Y-%m-%dT%H-%M-%SZ"


class AuditLogger:
    """Append-only JSONL audit log.

    One line per event. Every record includes a UTC ISO-8601 timestamp and
    the supplied `event` tag, plus any keyword fields the caller passes.
    Writes are serialized through a process-local lock so concurrent
    handler tasks do not interleave bytes within a line.

    Rotation (issue #28) is checked on every `log()` call inside the
    same lock as the write — atomic with respect to other writers,
    no race window where bytes could be written to a file that's
    about to be renamed.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        rotation: AuditRotationConfig | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._clock = clock
        # Importing the schema here would create a config↔audit cycle;
        # we accept a duck-typed value or None instead. None disables
        # rotation entirely (matches Phase 1-7 behavior).
        self._rotation = rotation
        # Pre-computed next rotation boundary, advanced on each
        # rotation. None when time-based rotation is off.
        self._next_rotation_at: datetime | None = self._compute_next_rotation(self._clock())

        if rotation is not None and rotation.max_bytes == 0 and rotation.when == "off":
            logger.warning(
                "audit log rotation disabled (max_bytes=0 AND when='off') — "
                "active file %s will grow without bound. Use external rotation "
                "or enable a rotation trigger in app.yaml under audit.rotation.",
                self._path,
            )

    @property
    def path(self) -> Path:
        return self._path

    def log(self, event: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": self._clock().isoformat(),
            "event": event,
        }
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        encoded = line.encode("utf-8")

        with self._lock:
            # Rotation check must run inside the lock so a rotation
            # firing during a write doesn't lose the in-flight line
            # to the renamed file. After rotation, the next open(...)
            # creates a fresh path — encoded bytes go to the new file.
            self._maybe_rotate(now=self._clock(), pending_bytes=len(encoded))
            with self._path.open("ab") as fh:
                fh.write(encoded)

    # --- rotation internals -----------------------------------------

    def _maybe_rotate(self, *, now: datetime, pending_bytes: int) -> None:
        """Decide whether to rotate and, if so, perform the rename +
        compress + prune. Caller must hold `self._lock`.

        Either trigger (size or time) firing causes rotation.
        """
        if self._rotation is None:
            return
        rot = self._rotation

        # Time trigger: now ≥ next boundary
        time_due = self._next_rotation_at is not None and now >= self._next_rotation_at

        # Size trigger: existing size + new bytes would exceed cap.
        # max_bytes=0 disables size-based rotation.
        size_due = False
        if rot.max_bytes > 0 and self._path.exists():
            current_size = self._path.stat().st_size
            if current_size + pending_bytes > rot.max_bytes:
                size_due = True

        if not (time_due or size_due):
            return

        # Empty file — nothing useful to rotate, just advance the
        # time boundary and move on. Avoids creating empty
        # `audit.jsonl.<ts>` files when the bot is idle past midnight.
        if not self._path.exists() or self._path.stat().st_size == 0:
            if time_due:
                self._next_rotation_at = self._compute_next_rotation(now)
            return

        try:
            self._do_rotate(now=now, compress=rot.compress)
        except OSError:
            # Rotation failure (rename collision, disk full mid-rename,
            # etc.) shouldn't drop the in-flight write — log to the
            # bot's own log channel and continue appending to the
            # (now oversized) active file. The operator sees the
            # warning in `bot-cmder serve` output.
            logger.exception("audit log rotation failed for %s; continuing to append", self._path)
        else:
            if rot.backup_count > 0:
                try:
                    self._prune(rot.backup_count)
                except OSError:
                    logger.exception("audit log prune failed in %s", self._path.parent)

        if time_due:
            self._next_rotation_at = self._compute_next_rotation(now)

    def _do_rotate(self, *, now: datetime, compress: bool) -> None:
        """Rename the active file and (optionally) gzip it."""
        suffix = now.strftime(_TS_FORMAT)
        rotated = self._path.with_name(f"{self._path.name}.{suffix}")
        # If we already rotated this second (rapid size triggers, or a
        # contrived test), append a counter so we never silently
        # overwrite a prior rotation's data.
        counter = 1
        while rotated.exists() or rotated.with_suffix(rotated.suffix + ".gz").exists():
            rotated = self._path.with_name(f"{self._path.name}.{suffix}.{counter}")
            counter += 1

        os.rename(self._path, rotated)

        if compress:
            gz_path = rotated.with_suffix(rotated.suffix + ".gz")
            try:
                with rotated.open("rb") as src, gzip.open(gz_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except OSError:
                # Compression failed mid-write — leave both the partial
                # .gz and the plain rotated file behind. Operator
                # sees the warning, can investigate. Data preserved
                # in the plain file regardless.
                logger.exception("audit log compression failed for %s", rotated)
                gz_path.unlink(missing_ok=True)
            else:
                rotated.unlink()

    def _prune(self, backup_count: int) -> None:
        """Unlink the oldest rotated files past `backup_count`.

        Match BOTH `<name>.<ts>` and `<name>.<ts>.gz` (mixed
        compression states tolerated for migration scenarios). Sort
        by mtime ascending; oldest go first.
        """
        rotated = list(self._path.parent.glob(f"{self._path.name}.*"))
        rotated = [p for p in rotated if p.is_file() and p != self._path]
        rotated.sort(key=lambda p: p.stat().st_mtime)
        if len(rotated) <= backup_count:
            return
        for old in rotated[: len(rotated) - backup_count]:
            try:
                old.unlink()
            except OSError:
                logger.warning("failed to prune old audit log %s", old, exc_info=True)

    def _compute_next_rotation(self, now: datetime) -> datetime | None:
        """Return the next time-based rotation boundary, or None if
        time-based rotation is off."""
        if self._rotation is None:
            return None
        when = self._rotation.when
        if when == "off":
            return None

        # Normalize to UTC if a naive `now` slipped in (shouldn't, but
        # defensive — the rest of the bot uses tz-aware UTC).
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        if when == "hourly":
            # Top of next hour
            anchor = now.replace(minute=0, second=0, microsecond=0)
            return anchor + timedelta(hours=1)
        if when in ("daily", "midnight"):
            # Next UTC midnight. "daily" anchored to midnight is the
            # forensics-query-friendly default.
            anchor = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return anchor + timedelta(days=1)
        if when == "weekly":
            # Next Monday at 00:00 UTC. Monday=0 in `weekday()`.
            days_until_monday = (7 - now.weekday()) % 7 or 7
            anchor = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return anchor + timedelta(days=days_until_monday)
        # Should be impossible — schema validator catches this at
        # config load time. Defensive return so audit doesn't become
        # a config-validation chokepoint.
        logger.warning("unknown audit rotation 'when' value %r — disabling time trigger", when)
        return None
