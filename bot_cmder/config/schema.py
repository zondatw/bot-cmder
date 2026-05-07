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


class AuditRotationConfig(BaseModel):
    """Issue #28 — built-in audit log rotation.

    Two-axis trigger: rotate when the active file passes `max_bytes`
    OR when the wall clock crosses the `when` boundary, whichever
    fires first. Rotated files renamed to `audit.jsonl.<ts>` (UTC,
    filesystem-safe), gzipped if `compress=True`, and the oldest
    pruned past `backup_count`.

    Defaults are tuned for a typical SRE deployment:
        * 100 MB size cap → reasonable forensic file size
        * daily midnight UTC rotation → matches "what happened on date X"
          query phrasing
        * 7 backups → one week of history at daily rotation
        * gzip on → JSONL compresses ~10x, retention budget stretches

    Disable rotation entirely by setting `max_bytes=0` AND `when="off"`.
    """

    # Active file size at which rotation fires. 0 disables size-based
    # rotation (then `when` must be set to anything other than "off").
    max_bytes: int = 100_000_000

    # Time-based trigger. One of:
    #   "off"      — no time-based rotation (only size triggers)
    #   "hourly"   — rotate at top of every UTC hour
    #   "daily"    — rotate every 24h after the first write
    #   "midnight" — rotate at UTC 00:00 (alias for "daily" anchored
    #                to wall-clock midnight, common forensics phrasing)
    #   "weekly"   — rotate Mondays at UTC 00:00
    when: str = "midnight"

    # Number of rotated files to retain before unlinking the oldest.
    # 0 disables pruning (let the operator + filesystem fill up — risky
    # but matches the pre-rotation behavior on a renamed file).
    backup_count: int = 7

    # Gzip rotated files. JSONL compresses ~10x; cheap CPU at rotation
    # time, big disk savings. Off if you want rotated files immediately
    # `tail -f`-able without `gunzip -c`.
    compress: bool = True

    @model_validator(mode="after")
    def _check_when_and_max_bytes(self) -> AuditRotationConfig:
        valid_when = {"off", "hourly", "daily", "midnight", "weekly"}
        if self.when not in valid_when:
            raise ValueError(f"audit.rotation.when must be one of {sorted(valid_when)}, got {self.when!r}")
        if self.max_bytes < 0:
            raise ValueError(f"audit.rotation.max_bytes must be >= 0, got {self.max_bytes}")
        if self.backup_count < 0:
            raise ValueError(f"audit.rotation.backup_count must be >= 0, got {self.backup_count}")
        if self.max_bytes == 0 and self.when == "off":
            # Both triggers disabled — explicit, but worth surfacing
            # so an operator who set `when: off` and forgot to also
            # set max_bytes doesn't end up with infinite single-file
            # growth thinking they configured rotation.
            # We don't raise (it's a valid configuration — "I really
            # do want unbounded growth, e.g. external rotation handles
            # it"), but the AuditLogger logs a warning at startup.
            pass
        return self


class AuditConfig(_NullableDefaults):
    # Default resolves at instantiation time, not class-definition time,
    # so changing CWD or env vars between import and AppConfig() works
    # as expected. Issue #20: dev workflow keeps writing to ./var/ when
    # ./var/ exists; installed users get $XDG_STATE_HOME/bot-cmder/.
    path: Path = Field(default_factory=lambda: state_dir() / "audit.jsonl")
    # Issue #28 — rotation config. Default: daily midnight UTC, 100 MB
    # size cap, 7 backups, gzipped. Override fields individually in
    # app.yaml under `audit.rotation:`; omit the block entirely to
    # take the defaults.
    rotation: AuditRotationConfig = Field(default_factory=AuditRotationConfig)


class OTPLockoutConfig(BaseModel):
    """Issue #33 — OTP brute-force lockout policy.

    Per-norm_id state machine. After `max_failures` consecutive
    `OTP_INVALID` events within `failure_window_minutes`, the user
    gets locked out for `lockout_minutes`. During lockout EVERY
    `/otp <code>` is rejected (audit `OTP_LOCKED_OUT`) — even if
    the code is correct — so an attacker who eventually stumbles
    onto the right code still can't burn through. Successful OTP
    outside lockout resets the failure count.

    Defaults are tuned for a typical SRE incident: 5 attempts gives
    legitimate users plenty of typo room; 10-min lockout is short
    enough to wait through during a real incident, long enough to
    crush attacker ROI.

    Per-norm_id scoping: locking `slack:U0X...` does NOT lock
    `telegram:111`, even when both are aliases of the same `id:`
    in `users:`. Matches the existing OTP enrollment scoping —
    locking out compromised platforms while keeping the un-
    compromised ones usable for incident response.
    """

    # Master switch. Default ON — security-by-default, matches the
    # README's "strong defaults you can live with" pitch. Set to
    # false for solo home labs / dogfood environments / specific
    # SREs who want every failure visible in audit without lockout
    # cutting attempts short.
    enabled: bool = True

    # Failure count that triggers lockout. 5 leaves typo headroom
    # without being lax.
    max_failures: int = 5

    # Lockout duration once triggered.
    lockout_minutes: int = 10

    # Sliding window over which failures are counted. Failures older
    # than this fall out of the count, so a single typo at 9am
    # followed by another at 3pm doesn't pile up.
    failure_window_minutes: int = 15

    @model_validator(mode="after")
    def _check_positive(self) -> OTPLockoutConfig:
        if self.max_failures < 1:
            raise ValueError(f"totp.lockout.max_failures must be >= 1, got {self.max_failures}")
        if self.lockout_minutes < 1:
            raise ValueError(f"totp.lockout.lockout_minutes must be >= 1, got {self.lockout_minutes}")
        if self.failure_window_minutes < 1:
            raise ValueError(f"totp.lockout.failure_window_minutes must be >= 1, got {self.failure_window_minutes}")
        return self


class TOTPConfig(_NullableDefaults):
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
    # Issue #33 — brute-force lockout. Default ON; tune in app.yaml
    # under `totp.lockout:` or omit the block entirely to take the
    # defaults. Set `enabled: false` to opt out of lockout entirely
    # (the bot still records OTP_INVALID, just doesn't act on the
    # count).
    lockout: OTPLockoutConfig = Field(default_factory=OTPLockoutConfig)


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
