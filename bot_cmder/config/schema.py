from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, HttpUrl


class HealthTarget(BaseModel):
    name: str
    url: HttpUrl
    expect_status: int = 200
    timeout_s: float = 5.0


class HealthcheckConfig(BaseModel):
    targets: list[HealthTarget] = Field(default_factory=list)


class UserConfig(BaseModel):
    id: str
    aliases: list[str] = Field(default_factory=list)
    role: str = "viewer"


class ACLConfig(BaseModel):
    default_allow_safe: list[str] = Field(default_factory=list)
    commands: dict[str, list[str]] = Field(default_factory=dict)


class AuditConfig(BaseModel):
    path: Path = Path("./var/audit.jsonl")


class AppConfig(BaseModel):
    users: list[UserConfig] = Field(default_factory=list)
    acl: ACLConfig = Field(default_factory=ACLConfig)
    healthcheck: HealthcheckConfig = Field(default_factory=HealthcheckConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)

    def role_members(self, role: str) -> set[str]:
        members: set[str] = set()
        for u in self.users:
            if u.role == role:
                members.update(u.aliases)
        return members

    def alias_to_user(self) -> dict[str, UserConfig]:
        out: dict[str, UserConfig] = {}
        for u in self.users:
            for a in u.aliases:
                out[a] = u
        return out

    @classmethod
    def from_yaml(cls, path: Path | str) -> AppConfig:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)
