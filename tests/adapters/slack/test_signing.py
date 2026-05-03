"""Tests for Slack request-signature verification.

Standalone from the FastAPI router so the HMAC math + replay window
+ version check are pinned independently of HTTP plumbing.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from bot_cmder.adapters.slack.signing import (
    SIGNATURE_VERSION,
    SlackSignatureError,
    verify_slack_signature,
)

SECRET = "shhhh-keep-it-secret"


def _sign(body: bytes, ts: str, secret: str = SECRET) -> str:
    """Mirror Slack's signing exactly so tests can produce valid headers."""
    base = f"{SIGNATURE_VERSION}:{ts}:".encode() + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def test_accepts_valid_signature_within_window():
    body = b"command=/help&user_id=U1"
    ts = "1700000000"
    sig = _sign(body, ts)
    # now_s injected so the test isn't time-dependent.
    verify_slack_signature(
        signing_secret=SECRET,
        timestamp=ts,
        signature=sig,
        body=body,
        now_s=1700000010,  # 10s drift, well inside window
    )


def test_rejects_missing_headers():
    with pytest.raises(SlackSignatureError, match="missing"):
        verify_slack_signature(signing_secret=SECRET, timestamp="", signature="", body=b"")


def test_rejects_non_integer_timestamp():
    with pytest.raises(SlackSignatureError, match="not an integer"):
        verify_slack_signature(
            signing_secret=SECRET,
            timestamp="not-a-number",
            signature="v0=00",
            body=b"x",
        )


def test_rejects_replay_outside_window():
    body = b"x"
    ts = "1700000000"
    sig = _sign(body, ts)
    with pytest.raises(SlackSignatureError, match="drift"):
        verify_slack_signature(
            signing_secret=SECRET,
            timestamp=ts,
            signature=sig,
            body=body,
            now_s=1700000000 + 60 * 10,  # 10 min later, outside default 5
        )


def test_rejects_unsupported_signature_version():
    body = b"x"
    ts = "1700000000"
    # Pretend Slack moved to v1; we still only accept v0.
    bad_sig = "v1=" + "0" * 64
    with pytest.raises(SlackSignatureError, match="version"):
        verify_slack_signature(
            signing_secret=SECRET,
            timestamp=ts,
            signature=bad_sig,
            body=body,
            now_s=1700000010,
        )


def test_rejects_signature_signed_with_wrong_secret():
    body = b"command=/help"
    ts = "1700000000"
    forged = _sign(body, ts, secret="attacker-guessed-this")
    with pytest.raises(SlackSignatureError, match="HMAC"):
        verify_slack_signature(
            signing_secret=SECRET,
            timestamp=ts,
            signature=forged,
            body=body,
            now_s=1700000010,
        )


def test_signature_includes_body_bytes_exactly():
    """A one-byte body change must invalidate the signature.

    Regression guard against accidentally hashing only the body length
    or a stripped/normalized body.
    """
    body = b"command=/help&text=ok"
    ts = "1700000000"
    sig = _sign(body, ts)
    # Verify same body passes
    verify_slack_signature(signing_secret=SECRET, timestamp=ts, signature=sig, body=body, now_s=1700000010)
    # Single-byte mutation must fail
    with pytest.raises(SlackSignatureError, match="HMAC"):
        verify_slack_signature(
            signing_secret=SECRET,
            timestamp=ts,
            signature=sig,
            body=body + b"!",
            now_s=1700000010,
        )
