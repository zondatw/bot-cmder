"""End-to-end smoke for create_app().

Catches the class of bug where main.py wires install_all() with a
new keyword argument but bot_cmder/commands/builtin/__init__.py's
install_all() signature wasn't updated to match. Unit tests for the
individual builtins bypass install_all() (they call each module's
install() directly), so a signature drift between the two is
invisible to them — but lands as a TypeError at uvicorn startup.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch):
    """Point Settings + AppConfig at a tmp dir so we don't touch real
    .env / config/app.yaml / var/."""
    monkeypatch.setenv("BOT_CMDER_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "missing.yaml"))
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)

    # The settings cache is process-wide; clear it so the env vars we
    # just set actually take effect this call.
    from bot_cmder.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_create_app_does_not_crash_without_telegram_token():
    """Regression: install_all() must accept every keyword main.py
    passes — TOTP triad AND ssh_pool. A signature drift here lands
    as `TypeError: install_all() got an unexpected keyword argument`
    at uvicorn startup, which the unit tests don't see because they
    skip install_all() entirely."""
    from bot_cmder.main import create_app

    app = create_app()
    # /healthz mounts unconditionally; /webhooks/telegram only when
    # TELEGRAM_TOKEN is set (it isn't in this fixture).
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/healthz" in paths
    assert "/webhooks/telegram" not in paths


def test_create_app_registers_every_builtin():
    """Regression: install_all() must register every Phase 1 + 2 + 3
    builtin into the registry — not silently skip ones whose install
    function isn't wired into install_all()."""
    from bot_cmder.audit.log import AuditLogger
    from bot_cmder.auth.pending import PendingOTPSessions
    from bot_cmder.auth.secret_store import SecretStore
    from bot_cmder.auth.totp import TOTPVerifier
    from bot_cmder.commands.builtin import install_all
    from bot_cmder.config.schema import SshConnectorConfig
    from bot_cmder.connectors.ssh import SshConnectorPool
    from bot_cmder.core.registry import CommandRegistry

    audit = AuditLogger("/tmp/_audit.jsonl")  # noqa: S108
    store = SecretStore("/tmp/_t.sqlite", Fernet.generate_key().decode())  # noqa: S108
    totp = TOTPVerifier(store)
    pending = PendingOTPSessions()
    pool = SshConnectorPool({}, SshConnectorConfig())

    reg = CommandRegistry()
    install_all(reg, pending=pending, totp=totp, audit=audit, ssh_pool=pool)

    names = {c.name for c in reg.all()}
    assert names == {
        # Phase 1
        "help",
        "whoami",
        "health",
        # Phase 2
        "kubectl",
        "runbook_list",
        "runbook_run",
        "otp",
        # Phase 3
        "ssh",
        "service_list",
        "service_status",
        "service_restart",
        "service_logs",
    }
