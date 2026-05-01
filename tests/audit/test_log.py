from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from bot_cmder.audit.log import AuditLogger


def test_writes_one_jsonl_record_per_log(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    log = AuditLogger(path)
    log.log("EXECUTED", user="telegram:1", command="ping")
    log.log("AUTH_DENIED", user="telegram:2", command="restart")

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    a = json.loads(lines[0])
    b = json.loads(lines[1])
    assert a["event"] == "EXECUTED"
    assert a["user"] == "telegram:1"
    assert b["event"] == "AUTH_DENIED"


def test_includes_iso_utc_timestamp(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    fixed = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    log = AuditLogger(path, clock=lambda: fixed)
    log.log("X")
    rec = json.loads(path.read_text())
    assert rec["ts"] == fixed.isoformat()


def test_creates_parent_directory(tmp_path: Path):
    path = tmp_path / "nested" / "deep" / "a.jsonl"
    AuditLogger(path).log("HELLO")
    assert path.exists()


def test_serializes_nonjson_values_via_str(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    log = AuditLogger(path)
    log.log("X", payload={"args": ["a", "b"]}, when=datetime(2026, 1, 1))
    rec = json.loads(path.read_text())
    assert rec["payload"] == {"args": ["a", "b"]}
    assert "2026" in rec["when"]
