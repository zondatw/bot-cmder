from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bot_cmder.config.schema import AppConfig


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "app.yaml"
    path.write_text(textwrap.dedent(body))
    return path


def test_yaml_with_null_commands_loads_as_empty_dict(tmp_path: Path):
    """Regression: when every entry under `acl.commands:` is commented
    out, YAML parses the value as null. The schema must treat that the
    same as omitting the key entirely (empty dict), instead of failing
    validation with `Input should be a valid dictionary`."""
    path = _write(
        tmp_path,
        """
        acl:
          default_allow_safe: ["role:sre"]
          commands:
            # restart: ["role:sre"]
        """,
    )
    cfg = AppConfig.from_yaml(path)
    assert cfg.acl.commands == {}


def test_yaml_with_explicit_empty_commands_loads(tmp_path: Path):
    path = _write(
        tmp_path,
        """
        acl:
          default_allow_safe: ["role:sre"]
          commands: {}
        """,
    )
    cfg = AppConfig.from_yaml(path)
    assert cfg.acl.commands == {}


def test_omitting_acl_section_uses_defaults(tmp_path: Path):
    path = _write(tmp_path, "")
    cfg = AppConfig.from_yaml(path)
    assert cfg.acl.commands == {}
    assert cfg.acl.default_allow_safe == []
    assert cfg.users == []
    assert cfg.healthcheck.targets == []


def test_null_users_treated_as_empty(tmp_path: Path):
    path = _write(
        tmp_path,
        """
        users:
        acl:
          default_allow_safe: ["role:sre"]
        """,
    )
    cfg = AppConfig.from_yaml(path)
    assert cfg.users == []


@pytest.mark.parametrize(
    "snippet",
    [
        "healthcheck:\n  targets:\n",
        "healthcheck:\n",
        "",
    ],
)
def test_null_or_missing_healthcheck_targets(tmp_path: Path, snippet: str):
    path = _write(tmp_path, snippet)
    cfg = AppConfig.from_yaml(path)
    assert cfg.healthcheck.targets == []
