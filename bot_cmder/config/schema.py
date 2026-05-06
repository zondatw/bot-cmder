from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, HttpUrl, model_validator

from bot_cmder.config.paths import state_dir


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
    # Default resolves at instantiation time, not class-definition time,
    # so changing CWD or env vars between import and AppConfig() works
    # as expected. Issue #20: dev workflow keeps writing to ./var/ when
    # ./var/ exists; installed users get $XDG_STATE_HOME/bot-cmder/.
    path: Path = Field(default_factory=lambda: state_dir() / "audit.jsonl")


class TOTPConfig(BaseModel):
    """Phase 2 TOTP-gating settings."""

    # Same default_factory pattern as AuditConfig.path — see issue #20.
    # Existing yaml that hardcodes `./var/totp.sqlite` keeps working;
    # only the implicit default changes for users who never set it.
    secret_store_path: Path = Field(default_factory=lambda: state_dir() / "totp.sqlite")
    # Lifetime of a "pending privileged command awaiting OTP" session.
    # Telegram users get this many seconds to reply with `/otp <code>`
    # before the session expires and they have to rerun the command.
    session_ttl_s: int = 120
    # Issue #15 — hard cap on the duration `/otp emergency <minutes>`
    # can request. Even if the operator types a larger number, the
    # opened window is capped to this. Rationale: incident lasting
    # >60 min has more cost than a fresh re-auth round, and a stolen
    # chat session during a bypass window is bounded in damage by
    # this number. Tunable per deployment but defaults are
    # deliberately conservative.
    emergency_max_minutes: int = 60


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
    """Phase 2 /runbook list and /runbook run builtin settings."""

    dir: Path = Path("./runbooks")
    max_output_bytes: int = 3500


class HostSpec(_NullableDefaults):
    """Phase 3 — one SSH-reachable target.

    `address` may be a hostname or an IP. `user` is the remote login
    account; the bot never uses passwords, only the key at `key_path`.

    `known_hosts`: explicit path to a known_hosts file. When unset,
    asyncssh falls back to `~/.ssh/known_hosts` for the bot's user.
    Strict host key checking is always on — a missing entry is a
    fatal connection error, not a TOFU prompt.

    `allowed_commands` is a list of Python regex patterns. The /ssh
    escape hatch refuses to run any command that doesn't match one
    of these for the target host. /service actions bypass this list
    because the action templates themselves are the allowlist.
    """

    address: str
    user: str
    port: int = 22
    key_path: Path | None = None
    known_hosts: Path | None = None
    allowed_commands: list[str] = Field(default_factory=list)


class ServiceSpec(_NullableDefaults):
    """Phase 3 — a service with predefined actions across one or more hosts.

    `hosts` references HostSpec keys in `AppConfig.hosts`. `actions`
    maps an action name (`status` / `restart` / `logs` / ...) to the
    literal shell command line that gets executed on the remote host.
    Action commands are never templated with user input — they're the
    same string for every invocation, which is why /service skips the
    per-host `allowed_commands` regex check.
    """

    hosts: list[str] = Field(default_factory=list)
    actions: dict[str, str] = Field(default_factory=dict)


class SlackConfig(_NullableDefaults):
    """Phase 5 Slack adapter settings.

    Slack's slash command flow can reply two ways:
      - `ephemeral` — only the invoker sees it (private, default-safe)
      - `in_channel` — the whole channel sees it (great for collaborative
        ops: teammates can spot each other's status checks and reduce
        "what's going on right now?" anxiety without re-asking)

    `reply_visibility` is the global default; set per command via
    `visibility_overrides`. The `by_risk` mode is the suggested middle
    ground — diagnostic SAFE commands (`/help`, `/health`, `/service status`)
    go to the channel so the team learns by lurking; PRIVILEGED ones
    (`/service restart`, `/ssh`, `/kubectl`) stay private so SSH output
    isn't broadcast.

    Override keys are the synthetic command names the dispatcher sees
    AFTER router rewrite (e.g. `service_restart`, not `service restart`).
    Same name `audit.jsonl` records under `command:` — easy to copy.
    """

    # Default reply visibility. One of "by_risk", "in_channel",
    # "ephemeral". `by_risk` maps SAFE → in_channel, PRIVILEGED →
    # ephemeral. Anything outside the three is rejected at load time.
    reply_visibility: str = "by_risk"

    # Per-command visibility override. Value must be "in_channel" or
    # "ephemeral" (no `by_risk` here — overrides resolve to one of the
    # two final states). Unknown commands are tolerated so a yaml
    # carried over from a removed action doesn't crash startup.
    visibility_overrides: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_visibility_values(self) -> SlackConfig:
        if self.reply_visibility not in {"by_risk", "in_channel", "ephemeral"}:
            raise ValueError(
                f"slack.reply_visibility must be one of by_risk / in_channel / ephemeral, got {self.reply_visibility!r}"
            )
        for cmd, mode in self.visibility_overrides.items():
            if mode not in {"in_channel", "ephemeral"}:
                raise ValueError(
                    f"slack.visibility_overrides[{cmd!r}] must be 'in_channel' or 'ephemeral', got {mode!r}"
                )
        return self


class SshConnectorConfig(BaseModel):
    """Phase 3 SshConnector tuning."""

    # Reuse one SSHClientConnection per host for this many seconds
    # before tearing it down and reopening on the next call. Avoids
    # a fresh handshake per command without holding a stale socket
    # open forever.
    pool_ttl_s: int = 300
    # Per-call output cap; longer SSH output is truncated.
    max_output_bytes: int = 3500
    # Hard timeout on each individual remote command.
    command_timeout_s: int = 30


class AppConfig(_NullableDefaults):
    users: list[UserConfig] = Field(default_factory=list)
    acl: ACLConfig = Field(default_factory=ACLConfig)
    healthcheck: HealthcheckConfig = Field(default_factory=HealthcheckConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    totp: TOTPConfig = Field(default_factory=TOTPConfig)
    kubectl: KubectlConfig = Field(default_factory=KubectlConfig)
    runbook: RunbookConfig = Field(default_factory=RunbookConfig)
    # Phase 3 — SSH connector + /service / /ssh builtins
    hosts: dict[str, HostSpec] = Field(default_factory=dict)
    services: dict[str, ServiceSpec] = Field(default_factory=dict)
    ssh: SshConnectorConfig = Field(default_factory=SshConnectorConfig)
    # Phase 5 — Slack adapter knobs (visibility / overrides)
    slack: SlackConfig = Field(default_factory=SlackConfig)

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
