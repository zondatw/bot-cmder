"""Tests for `bot_cmder.core.redact` — credential-redaction policy.

Centralizing the rules in one module is only valuable if every code
path actually goes through it AND the rules are pinned by tests.
The other half of the contract — "every audit/recv log site uses
the helper" — is covered by integration regression tests in
test_dispatcher.py and the per-adapter test files.
"""

from __future__ import annotations

import pytest

from bot_cmder.core.redact import (
    REDACTED_PLACEHOLDER,
    is_sensitive_command,
    redact_args_for_audit,
    redact_text,
)

# --- is_sensitive_command --------------------------------------------


def test_otp_is_sensitive():
    assert is_sensitive_command("otp")


def test_other_commands_are_not_sensitive():
    """If you're adding a new builtin and it's a credential, the test
    suite should fail in this list — forcing a deliberate review."""
    for name in ("help", "whoami", "health", "kubectl", "service_restart", "ssh", "runbook_run"):
        assert not is_sensitive_command(name), f"{name} should NOT be in sensitive set"


# --- redact_args_for_audit -------------------------------------------


def test_otp_args_replaced_with_placeholder():
    """OTP code must NEVER reach audit.jsonl — even though it's
    short-lived, it's still a credential while alive AND if a user
    fat-fingers the enrollment URI as the arg, the BASE32 secret
    would land in audit forever."""
    args = ["918493"]
    out = redact_args_for_audit("otp", args)
    assert out == [REDACTED_PLACEHOLDER]
    assert "918493" not in str(out)


def test_otp_args_with_url_redacted_too():
    """Real-world dogfood incident: user typed `/otp <enrollment-uri>`
    by mistake, the entire BASE32 secret landed in audit. The whole
    args list goes regardless of shape."""
    args = ["otpauth://totp/bot-cmder:user?secret=ABCDEFGHIJKLMNOP&issuer=bot-cmder"]
    out = redact_args_for_audit("otp", args)
    assert out == [REDACTED_PLACEHOLDER]
    assert "secret" not in str(out)
    assert "ABCDEFGHIJKLMNOP" not in str(out)


def test_non_sensitive_command_args_pass_through():
    args = ["hello", "--host", "gce"]
    assert redact_args_for_audit("service_restart", args) == args


def test_empty_args_for_sensitive_still_redacted():
    """Defense in depth — even if the args list is empty, return the
    placeholder so a future caller that does `args=...` can't leak
    by accident."""
    assert redact_args_for_audit("otp", []) == [REDACTED_PLACEHOLDER]


# --- redact_text -----------------------------------------------------


def test_otp_text_keeps_command_drops_args():
    """Operators still see WHAT was issued (`/otp`) but not the code."""
    assert redact_text("/otp 918493") == f"/otp {REDACTED_PLACEHOLDER}"


def test_otp_with_url_arg_redacted():
    out = redact_text("/otp otpauth://totp/bot-cmder:u?secret=ABCD&issuer=bot-cmder")
    assert out == f"/otp {REDACTED_PLACEHOLDER}"
    assert "secret" not in out


def test_bare_otp_with_no_args_passes_through():
    """`/otp` alone has nothing to redact — the user just typed the
    command name and would benefit from seeing it (handler will
    return a usage hint)."""
    assert redact_text("/otp") == "/otp"


def test_non_command_text_passes_through():
    """A literal '/otp' inside a regular message body is not an
    invocation — startswith('/') gates it AND we don't try to be
    clever about message content."""
    assert redact_text("hello world") == "hello world"
    # Edge: leading slash but not a command shape — let it through.
    # (We consciously don't try to distinguish; pattern is "/cmd args".)


def test_other_commands_pass_through_unchanged():
    """Non-sensitive commands with args are untouched — operators
    need to see them in chat echoes for the transcript to make sense."""
    assert redact_text("/service restart hello --host gce") == "/service restart hello --host gce"
    assert redact_text("/ssh gce uptime") == "/ssh gce uptime"
    assert redact_text("/help") == "/help"


# --- parametric coverage of the matrix ------------------------------


@pytest.mark.parametrize(
    "typed,expected",
    [
        ("/otp 918493", f"/otp {REDACTED_PLACEHOLDER}"),
        ("/otp 000000", f"/otp {REDACTED_PLACEHOLDER}"),
        ("/otp anything-after-space-redacted", f"/otp {REDACTED_PLACEHOLDER}"),
        ("/help", "/help"),
        ("/service restart api --host gce", "/service restart api --host gce"),
    ],
)
def test_redact_text_matrix(typed, expected):
    assert redact_text(typed) == expected
