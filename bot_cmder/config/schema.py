from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, HttpUrl, model_validator


class _NullableDefaults(BaseModel):
    """Treat explicit YAML null as 'use the field's default' for any
    field that has a default_factory.

    This avoids the foot-gun where commenting out every entry under a
    YAML mapping key turns it into None, and pydantic then refuses the
    document because None is not a valid dict / list.
    """

    @model_validator(mode="before")
    @classmethod
    def _coerce_null_to_default(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for name, field in cls.model_fields.items():
            if data.get(name) is None and field.default_factory is not None:
                data[name] = field.default_factory()
        return data


class HealthTarget(BaseModel):
    name: str
    url: HttpUrl
    expect_status: int = 200
    timeout_s: float = 5.0


class HealthcheckConfig(_NullableDefaults):
    targets: list[HealthTarget] = Field(default_factory=list)


class UserConfig(_NullableDefaults):
    id: str
    aliases: list[str] = Field(default_factory=list)
    role: str = "viewer"


class ACLConfig(_NullableDefaults):
    default_allow_safe: list[str] = Field(default_factory=list)
    commands: dict[str, list[str]] = Field(default_factory=dict)


class AuditConfig(BaseModel):
    path: Path = Path("./var/audit.jsonl")


class AppConfig(_NullableDefaults):
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
