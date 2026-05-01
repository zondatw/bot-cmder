from __future__ import annotations

from typing import TYPE_CHECKING

from bot_cmder.commands.builtin import health, help, kubectl, otp, runbook, service, ssh, whoami
from bot_cmder.core.registry import CommandRegistry

if TYPE_CHECKING:
    from bot_cmder.audit.log import AuditLogger
    from bot_cmder.auth.pending import PendingOTPSessions
    from bot_cmder.auth.totp import TOTPVerifier
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
) -> None:
    """Register the /otp builtin. Requires the TOTP wiring."""
    otp.install(registry, pending=pending, totp=totp, audit=audit)


def install_ssh(
    registry: CommandRegistry,
    *,
    ssh_pool: SshConnectorPool,
    audit: AuditLogger,
) -> None:
    """Register /ssh + /service-* builtins. Requires an SshConnectorPool."""
    ssh.install(registry, ssh_pool=ssh_pool, audit=audit)
    service.install(registry, ssh_pool=ssh_pool, audit=audit)


def install_all(
    registry: CommandRegistry,
    *,
    pending: PendingOTPSessions | None = None,
    totp: TOTPVerifier | None = None,
    audit: AuditLogger | None = None,
    ssh_pool: SshConnectorPool | None = None,
) -> None:
    """One-shot installer used by main.py.

    Always registers the safe + local-privileged commands. /otp lands
    only when the TOTP triad is wired; /ssh and /service-* land only
    when an SshConnectorPool is supplied.
    """
    install_safe(registry)
    install_privileged(registry)
    if pending is not None and totp is not None and audit is not None:
        install_otp(registry, pending=pending, totp=totp, audit=audit)
    if ssh_pool is not None and audit is not None:
        install_ssh(registry, ssh_pool=ssh_pool, audit=audit)
