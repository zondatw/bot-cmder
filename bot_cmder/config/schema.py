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


class TOTPConfig(BaseModel):
    """Phase 2 TOTP-gating settings."""

    secret_store_path: Path = Path("./var/totp.sqlite")
    # Lifetime of a "pending privileged command awaiting OTP" session.
    # Telegram users get this many seconds to reply with `/otp <code>`
    # before the session expires and they have to rerun the command.
    session_ttl_s: int = 120


class KubectlConfig(BaseModel):
    """Phase 2 /kubectl builtin settings."""

    # If unset, the kubectl process inherits the bot's KUBECONFIG /
    # default ~/.kube/config. Override here to pin a specific file.
    kubeconfig: Path | None = None
    # Hard whitelist of subcommands the bot will exec. Anything else
    # is rejected before reaching the subprocess. Order doesn't matter.
    allowed_subcommands: list[str] = Field(
        default_factory=lambda: ["get", "describe", "logs", "rollout", "scale", "top"]
    )
    # Per-call output cap; longer kubectl output is truncated with a
    # `[...truncated N bytes]` marker.
    max_output_bytes: int = 3500


class RunbookConfig(BaseModel):
    """Phase 2 /runbook-list and /runbook-run builtin settings."""

    dir: Path = Path("./runbooks")
    max_output_bytes: int = 3500


class AppConfig(_NullableDefaults):
    users: list[UserConfig] = Field(default_factory=list)
    acl: ACLConfig = Field(default_factory=ACLConfig)
    healthcheck: HealthcheckConfig = Field(default_factory=HealthcheckConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    totp: TOTPConfig = Field(default_factory=TOTPConfig)
    kubectl: KubectlConfig = Field(default_factory=KubectlConfig)
    runbook: RunbookConfig = Field(default_factory=RunbookConfig)

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
