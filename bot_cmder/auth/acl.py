from __future__ import annotations

from typing import TYPE_CHECKING

from bot_cmder.core.registry import Command, Risk

if TYPE_CHECKING:
    from bot_cmder.config.schema import AppConfig


def expand_allowed(rules: list[str], config: AppConfig) -> set[str]:
    """Resolve allow rules to a flat set of norm_ids.

    Each rule is either a literal norm_id (e.g. "telegram:111") or the
    role syntax "role:<name>" which expands to all aliases of users
    bearing that role.
    """
    out: set[str] = set()
    for rule in rules:
        if rule.startswith("role:"):
            out.update(config.role_members(rule.removeprefix("role:")))
        else:
            out.add(rule)
    return out


def check_allowed(user_norm_id: str, cmd: Command, config: AppConfig) -> bool:
    """Decide whether user_norm_id may run `cmd` under the given config.

    Resolution order:
        1. cmd.allowed (set on the command itself) wins if not None.
        2. config.acl.commands[cmd.name] if present.
        3. config.acl.default_allow_safe for SAFE commands.
        4. PRIVILEGED commands deny by default if no rule matches.
    """
    rules = cmd.allowed
    if rules is None:
        rules = config.acl.commands.get(cmd.name)
    if rules is None:
        if cmd.risk == Risk.SAFE:
            rules = config.acl.default_allow_safe
        else:
            return False
    return user_norm_id in expand_allowed(rules, config)
