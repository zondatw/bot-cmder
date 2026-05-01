from bot_cmder.commands.builtin import health, help, whoami
from bot_cmder.core.registry import CommandRegistry


def install_all(registry: CommandRegistry) -> None:
    """Register every Phase 1 builtin into the given registry."""
    help.install(registry)
    whoami.install(registry)
    health.install(registry)
