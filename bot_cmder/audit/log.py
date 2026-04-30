from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLogger:
    """Append-only JSONL audit log.

    One line per event. Every record includes a UTC ISO-8601 timestamp and
    the supplied `event` tag, plus any keyword fields the caller passes.
    Writes are serialized through a process-local lock so concurrent
    handler tasks do not interleave bytes within a line.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._clock = clock

    @property
    def path(self) -> Path:
        return self._path

    def log(self, event: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": self._clock().isoformat(),
            "event": event,
        }
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
