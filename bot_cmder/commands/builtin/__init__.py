from __future__ import annotations

from typing import TYPE_CHECKING

from bot_cmder.commands.builtin import health, help, kubectl, otp, runbook, service, ssh, whoami
from bot_cmder.core.registry import CommandRegistry

if TYPE_CHECKING:
    from bot_cmder.audit.log import AuditLogger
    from bot_cmder.auth.emergency import EmergencyWindows
    from bot_cmder.auth.lockout import OTPLockoutState
    from bot_cmder.auth.pending import PendingOTPSessions
    from bot_cmder.auth.totp import TOTPVerifier
    from bot_cmder.config.schema import AppConfig
    from bot_cmder.connectors.ssh import SshConnectorPool


def install_safe(registry: CommandRegistry) -> None:
    """Register the commands that need no auth dependencies."""
    help.install(registry)
    whoami.install(registry)
    health.install(registry)


def install_privileged(registry: CommandRegistry) -> None:
    """Register the local-execution privileged commands.

    These get registered regardless of whether TOTP is wired up; the
    Dispatcher will refuse to invoke them if `pending` is None and
    they have requires_2fa, so an unconfigured deployment fails
    closed rather than open.
    """
    kubectl.install(registry)
    runbook.install(registry)


def install_otp(
    registry: CommandRegistry,
    *,
    pending: PendingOTPSessions,
    totp: TOTPVerifier,
    audit: AuditLogger,
    emergency: EmergencyWindows | None = None,
    lockout: OTPLockoutState | None = None,
) -> None:
    """Register the /otp builtin. Requires the TOTP wiring; the
    emergency-window store + lockout state are optional (issue #15 +
    issue #33 — kept optional so existing tests that build a
    registry without them still work)."""
    otp.install(registry, pending=pending, totp=totp, audit=audit, emergency=emergency, lockout=lockout)


def install_ssh(
    registry: CommandRegistry,
    *,
    ssh_pool: SshConnectorPool,
    audit: AuditLogger,
    config: AppConfig,
) -> None:
    """Register /ssh + /service * builtins. Requires an SshConnectorPool.

    `config` is needed so service.install() can scan config.services
    for the union of action names and dynamically register a
    /service <action> subcommand for each.
    """
    ssh.install(registry, ssh_pool=ssh_pool, audit=audit)
    service.install(registry, ssh_pool=ssh_pool, audit=audit, config=config)


def install_all(
    registry: CommandRegistry,
    *,
    pending: PendingOTPSessions | None = None,
    totp: TOTPVerifier | None = None,
    audit: AuditLogger | None = None,
    ssh_pool: SshConnectorPool | None = None,
    config: AppConfig | None = None,
    emergency: EmergencyWindows | None = None,
    lockout: OTPLockoutState | None = None,
) -> None:
    """One-shot installer used by main.py.

    Always registers the safe + local-privileged commands. /otp lands
    only when the TOTP triad is wired; /ssh and /service * land only
    when both an SshConnectorPool and an AppConfig are supplied
    (config drives the dynamic /service action subcommands).

    `emergency` (issue #15) and `lockout` (issue #33) are optional.
    When provided, /otp respectively grows the emergency sub-syntaxes
    and pre-checks lockout state before consuming pending sessions.
    """
    install_safe(registry)
    install_privileged(registry)
    if pending is not None and totp is not None and audit is not None:
        install_otp(
            registry,
            pending=pending,
            totp=totp,
            audit=audit,
            emergency=emergency,
            lockout=lockout,
        )
    if ssh_pool is not None and audit is not None and config is not None:
        install_ssh(registry, ssh_pool=ssh_pool, audit=audit, config=config)
