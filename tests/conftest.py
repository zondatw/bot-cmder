from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot_cmder.audit.log import AuditLogger
from bot_cmder.config.schema import ACLConfig, AppConfig, AuditConfig, HealthcheckConfig, UserConfig
from bot_cmder.core.events import IncomingMessage, Platform, PlatformUser


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def audit(audit_path: Path) -> AuditLogger:
    return AuditLogger(audit_path)


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        users=[
            UserConfig(id="zonda", aliases=["telegram:111"], role="sre"),
            UserConfig(id="alice", aliases=["telegram:222"], role="viewer"),
        ],
        acl=ACLConfig(
            default_allow_safe=["role:sre", "role:viewer"],
            commands={"restart": ["role:sre"]},
        ),
        healthcheck=HealthcheckConfig(),
        audit=AuditConfig(path=tmp_path / "audit.jsonl"),
    )


def make_msg(
    text: str,
    *,
    user_id: str = "111",
    platform: Platform = Platform.TELEGRAM,
    chat_id: str = "42",
    handle: str | None = "zondatw",
) -> IncomingMessage:
    return IncomingMessage(
        platform=platform,
        user=PlatformUser(platform=platform, raw_id=user_id, handle=handle),
        chat_id=chat_id,
        text=text,
        message_id="1",
        raw={},
        received_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def make_message():
    return make_msg
