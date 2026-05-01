"""TOTP enrollment + verification on top of pyotp.

Adds two things pyotp itself doesn't:

  - Persistence of per-user secrets via a SecretStore (Fernet+SQLite).
  - Replay protection: a code that matched step N is remembered, and
    any later verify call is rejected if it would match a step <= N
    for the same user. Without this, an OTP intercepted within its
    30s window could be reused.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pyotp

if TYPE_CHECKING:
    from bot_cmder.auth.secret_store import SecretStore


class TOTPVerifier:
    def __init__(self, store: SecretStore, *, valid_window: int = 1, issuer: str = "bot-cmder") -> None:
        self._store = store
        self._window = valid_window
        self._issuer = issuer
        # Per-user record of the highest TOTP step number that has
        # already been accepted. In-memory: replay protection resets
        # on bot restart, which is acceptable because by then the code
        # is well outside the valid window anyway.
        self._last_used_step: dict[str, int] = {}

    def is_enrolled(self, user_norm_id: str) -> bool:
        return self._store.get_secret(user_norm_id) is not None

    def enroll(self, user_norm_id: str) -> tuple[str, str]:
        """Generate a fresh base32 secret, persist it, return (secret, provisioning_uri).

        The provisioning_uri is meant to be displayed as a QR code or
        copy-pasted into Google Authenticator / 1Password / Authy.
        """
        secret = pyotp.random_base32()
        self._store.set_secret(user_norm_id, secret)
        uri = pyotp.TOTP(secret).provisioning_uri(name=user_norm_id, issuer_name=self._issuer)
        return secret, uri

    def verify(self, user_norm_id: str, code: str) -> bool:
        """True iff `code` is a currently-valid TOTP for the user and has not been used."""
        secret = self._store.get_secret(user_norm_id)
        if secret is None:
            return False
        code = code.strip()
        if not code.isdigit() or len(code) != 6:
            return False
        totp = pyotp.TOTP(secret)
        now_step = int(time.time() // totp.interval)
        for offset in range(-self._window, self._window + 1):
            step = now_step + offset
            if totp.at(step * totp.interval) == code:
                if step <= self._last_used_step.get(user_norm_id, -1):
                    return False  # replay within window
                self._last_used_step[user_norm_id] = step
                return True
        return False
