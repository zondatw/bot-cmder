from __future__ import annotations

import pytest

from bot_cmder.core.parser import parse


def test_parses_simple_command():
    p = parse("/health")
    assert p is not None
    assert p.name == "health"
    assert p.args == []


def test_parses_with_args():
    p = parse("/kubectl get pods")
    assert p is not None
    assert p.name == "kubectl"
    assert p.args == ["get", "pods"]


def test_quoted_args_kept_together():
    p = parse('/say "hello world" foo')
    assert p is not None
    assert p.name == "say"
    assert p.args == ["hello world", "foo"]


def test_strips_telegram_botname_suffix():
    p = parse("/cmd@my_bot foo")
    assert p is not None
    assert p.name == "cmd"
    assert p.args == ["foo"]


def test_unicode_args():
    p = parse("/echo 你好")
    assert p is not None
    assert p.args == ["你好"]


@pytest.mark.parametrize(
    "text",
    ["", "   ", "no slash", "/", "/@bot", '/cmd "unterminated'],
)
def test_returns_none_on_invalid_input(text: str):
    assert parse(text) is None


def test_strips_leading_trailing_whitespace():
    p = parse("  /ping  ")
    assert p is not None
    assert p.name == "ping"


def test_em_dash_autocorrect_is_normalized_back_to_double_hyphen():
    """Regression: iOS/macOS Smart Dashes turn the user-typed `--`
    into U+2014 em-dash, which makes `--host` arrive as `—host` and
    silently breaks every flag-taking command. parser must normalize
    em-dash and friends back to ASCII before shlex split."""
    p = parse("/service_restart hello —host gce")
    assert p is not None
    assert p.name == "service_restart"
    assert p.args == ["hello", "--host", "gce"]


def test_horizontal_bar_normalized_too():
    p = parse("/cmd ―flag value")
    assert p is not None
    assert p.args == ["--flag", "value"]


def test_en_dash_normalized_to_single_hyphen():
    p = parse("/cmd –short")
    assert p is not None
    assert p.args == ["-short"]
