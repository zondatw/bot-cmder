"""Slack request-signature verification (HMAC-SHA256).

Slack signs every incoming request with two headers:

  X-Slack-Request-Timestamp: <unix_seconds>
  X-Slack-Signature:         v0=<hex_hmac_sha256>

The HMAC is taken over the literal byte string `v0:<ts>:<body>` using
the app's "Signing Secret" (from the Slack app config) as the key.
The `v0:` prefix is a version tag — Slack reserves the right to bump
it; we currently accept only v0.

Two checks beyond the HMAC itself:

  - Replay window: timestamp must be within ±5 minutes of "now". Slack
    docs cite 5 minutes; we honor it. Drift past that almost always
    means a captured-and-replayed request.
  - Constant-time compare: never `==` for the digest — that leaks
    timing. Use `hmac.compare_digest`.

Pure-stdlib (`hmac` + `hashlib`), no `slack-sdk` dependency. The
function is split out from the FastAPI router so it can be unit-tested
without a request object.
"""

from __future__ import annotations

import hashlib
import hmac
import time

# Slack docs say 5 minutes; mirror that exactly.
DEFAULT_MAX_DRIFT_S = 60 * 5

# Only signature version we accept. Slack has not introduced v1 yet
# (as of late 2025) but if they do, this constant centralizes the bump.
SIGNATURE_VERSION = "v0"


class SlackSignatureError(Exception):
    """Raised when an incoming Slack request fails signature checks.

    The message is *not* surfaced to the caller (Slack ignores response
    bodies on 401) — it's only for the bot's own logs.
    """


def verify_slack_signature(
    *,
    signing_secret: str,
    timestamp: str,
    signature: str,
    body: bytes,
    now_s: float | None = None,
    max_drift_s: int = DEFAULT_MAX_DRIFT_S,
) -> None:
    """Raise SlackSignatureError unless the request is authentic and fresh.

    Returns None on success so callers can treat it as `assert-style`.
    Splitting into separate exceptions per failure mode would leak
    information to anyone hitting the endpoint with garbage; one type
    is fine — operators look at structured audit logs, not error text.
    """
    if not timestamp or not signature:
        raise SlackSignatureError("missing X-Slack-Request-Timestamp or X-Slack-Signature header")

    try:
        ts_int = int(timestamp)
    except ValueError as exc:
        raise SlackSignatureError("X-Slack-Request-Timestamp is not an integer") from exc

    current = now_s if now_s is not None else time.time()
    if abs(current - ts_int) > max_drift_s:
        raise SlackSignatureError(f"request timestamp drift {abs(current - ts_int):.0f}s exceeds {max_drift_s}s window")

    if not signature.startswith(f"{SIGNATURE_VERSION}="):
        raise SlackSignatureError(f"unsupported signature version (want {SIGNATURE_VERSION}=...)")

    expected_basestring = f"{SIGNATURE_VERSION}:{timestamp}:".encode() + body
    expected_digest = hmac.new(
        signing_secret.encode("utf-8"),
        expected_basestring,
        hashlib.sha256,
    ).hexdigest()
    expected_signature = f"{SIGNATURE_VERSION}={expected_digest}"

    if not hmac.compare_digest(expected_signature, signature):
        raise SlackSignatureError("HMAC mismatch")
