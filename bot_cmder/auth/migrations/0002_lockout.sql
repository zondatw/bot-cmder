-- Issue #33 — OTP brute-force lockout state.
--
-- Two tables, both keyed by user_norm_id (telegram:111, slack:U0X...,
-- discord:222). Per-norm_id scoping by design — locking one platform
-- does not lock another, matching the existing TOTP enrollment scope.
--
-- `otp_failures` is the rolling failure log. Each OTP_INVALID writes
-- one row; entries older than the configured failure_window_minutes
-- get treated as "fallen out of the window" by the state-machine
-- code (no auto-prune trigger here — a periodic prune in the SQL
-- layer would be a needless complication for a tiny per-user log).
--
-- The non-unique (user_norm_id, failed_at) index supports the
-- threshold-count query without making timestamp-collision a failure
-- mode (multiple sub-second failures from the same user simply
-- record as multiple rows, as you would expect from a "raw event log"
-- table).
--
-- `otp_lockouts` holds active lockout windows. Exactly one row per
-- locked norm_id (PRIMARY KEY enforces). Rows persist past expiry
-- until the next access — `is_locked()` detects expiry and removes
-- lazily. `bot-cmder unlock-totp` deletes the row directly.

CREATE TABLE otp_failures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_norm_id TEXT    NOT NULL,
    failed_at    TEXT    NOT NULL
);

CREATE INDEX idx_otp_failures_user_time ON otp_failures (user_norm_id, failed_at);

CREATE TABLE otp_lockouts (
    user_norm_id  TEXT PRIMARY KEY,
    locked_at     TEXT NOT NULL,
    locked_until  TEXT NOT NULL,
    failure_count INTEGER NOT NULL
);
