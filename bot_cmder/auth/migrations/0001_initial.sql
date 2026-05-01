-- Initial schema for the per-user TOTP secret store.
--
-- One row per normalized user id. `secret_encrypted` is Fernet
-- ciphertext over the base32 shared secret; the symmetric key
-- lives in the BOT_CMDER_MASTER_KEY env var, never in the DB.
CREATE TABLE totp_secrets (
    user_norm_id     TEXT    PRIMARY KEY,
    secret_encrypted BLOB    NOT NULL,
    created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
