from __future__ import annotations

from bot_cmder.auth.acl import check_allowed, expand_allowed
from bot_cmder.config.schema import ACLConfig, AppConfig, UserConfig
from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import OutgoingResponse
from bot_cmder.core.registry import Command, Risk


async def _noop(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
    return OutgoingResponse.text_reply("ok")


def _config() -> AppConfig:
    return AppConfig(
        users=[
            UserConfig(id="zonda", aliases=["telegram:111", "discord:dz"], role="sre"),
            UserConfig(id="alice", aliases=["telegram:222"], role="viewer"),
        ],
        acl=ACLConfig(
            default_allow_safe=["role:sre", "role:viewer"],
            commands={"restart": ["role:sre"]},
        ),
    )


def test_role_expands_to_member_norm_ids():
    cfg = _config()
    assert expand_allowed(["role:sre"], cfg) == {"telegram:111", "discord:dz"}


def test_literal_norm_id_passes_through():
    cfg = _config()
    assert expand_allowed(["telegram:777"], cfg) == {"telegram:777"}


def test_safe_default_allows_known_role():
    cfg = _config()
    cmd = Command(name="health", risk=Risk.SAFE, handler=_noop)
    assert check_allowed("telegram:111", cmd, cfg) is True
    assert check_allowed("telegram:222", cmd, cfg) is True


def test_safe_default_denies_unknown_user():
    cfg = _config()
    cmd = Command(name="health", risk=Risk.SAFE, handler=_noop)
    assert check_allowed("telegram:999", cmd, cfg) is False


def test_command_specific_acl_overrides_default():
    cfg = _config()
    cmd = Command(name="restart", risk=Risk.PRIVILEGED, handler=_noop)
    # 222 is viewer; restart is role:sre only
    assert check_allowed("telegram:111", cmd, cfg) is True
    assert check_allowed("telegram:222", cmd, cfg) is False


def test_explicit_command_allowed_wins_over_config():
    cfg = _config()
    cmd = Command(name="ping", risk=Risk.SAFE, handler=_noop, allowed=["telegram:999"])
    # config says default_allow_safe includes everyone but the explicit
    # list narrows to a single norm_id.
    assert check_allowed("telegram:111", cmd, cfg) is False
    assert check_allowed("telegram:999", cmd, cfg) is True


def test_privileged_denies_by_default_when_no_rule_matches():
    cfg = _config()
    cmd = Command(name="nuke", risk=Risk.PRIVILEGED, handler=_noop)
    assert check_allowed("telegram:111", cmd, cfg) is False
