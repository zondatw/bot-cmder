"""In-memory holding pen for privileged commands awaiting OTP.

When the dispatcher sees a `requires_2fa=True` command, it stashes
the parsed name + args here keyed by user norm_id, then asks the user
to reply with `/otp <code>`. The /otp builtin pops the session and
re-runs the original handler if the code validates.

Single-process only — fine for the MVP single-instance deployment.
A multi-instance setup would need to swap this for a Redis/SQLite
backing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock

from bot_cmder.core.events import Platform


@dataclass
class PendingOTPSession:
    user_norm_id: str
    chat_id: str
    platform: Platform
    command_name: str
    args: list[str]
    expires_at: datetime


class PendingOTPSessions:
    def __init__(
        self,
        ttl_s: int = 120,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._sessions: dict[str, PendingOTPSession] = {}
        self._lock = Lock()

    @property
    def ttl_s(self) -> int:
        return self._ttl_s

    def stash(
        self,
        *,
        user_norm_id: str,
        chat_id: str,
        platform: Platform,
        command_name: str,
        args: list[str],
    ) -> PendingOTPSession:
        """Record a privileged command waiting for OTP. Overwrites any prior session for the same user."""
        session = PendingOTPSession(
            user_norm_id=user_norm_id,
            chat_id=chat_id,
            platform=platform,
            command_name=command_name,
            args=list(args),
            expires_at=self._clock() + timedelta(seconds=self._ttl_s),
        )
        with self._lock:
            self._sessions[user_norm_id] = session
        return session

    def pop(self, user_norm_id: str) -> PendingOTPSession | None:
        """Remove and return the user's pending session, or None if there isn't one."""
        with self._lock:
            return self._sessions.pop(user_norm_id, None)

    def is_expired(self, session: PendingOTPSession) -> bool:
        return session.expires_at < self._clock()
