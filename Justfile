set dotenv-load := true
set positional-arguments := true

# List all tasks.
default:
    @just --list

# Install dependencies and pre-commit hooks.
install:
    uv sync
    pre-commit install --install-hooks

# Run the dev server with autoreload.
dev:
    RELOAD=1 uv run python server.py

# Bring up a cloudflared quick tunnel, write its URL into .env as
# TELEGRAM_HOOK_URL, register the webhook with Telegram, then keep
# the tunnel running. Each run gives a fresh random subdomain.
tunnel:
    bash scripts/tunnel.sh

# Bring up an ngrok tunnel on your reserved static domain. The URL
# is permanent across restarts, so DNS is always warm — much more
# reliable than `just tunnel` for daily dev. One-time setup
# instructions print on first run if NGROK_DOMAIN is not set.
tunnel-ngrok:
    bash scripts/tunnel_ngrok.sh

# Run the test suite.
test *args:
    uv run pytest "$@"

# Run the test suite with coverage.
test-cov:
    uv run pytest --cov

# Lint (ruff + black --check).
lint:
    uv run ruff check .
    uv run black --check --line-length 120 .

# Auto-fix lint and format.
fix:
    uv run ruff check --fix .
    uv run black --line-length 120 .

# Echo the env settings used by the bot. Secrets print as <set>/
# <unset> only — the value itself is never displayed (an earlier
# bash version of this leaked tokens because `${V:+x}${V:-y}`
# evaluates BOTH branches when V is set). Implemented in python so
# the secret/non-secret split is maintained in one place.
show-env-settings:
    @python3 scripts/show_env_settings.py

# Show current Telegram webhook state — URL, pending update count,
# last delivery error. Call this any time you suspect the webhook is
# stale or queueing up (re-run after `just tunnel` to confirm).
hook-status:
    @python3 scripts/hook_status.py

# Watch hook-status every 2s. Ctrl-C to stop.
hook-watch:
    watch -n 2 -t "just hook-status"

# Register the webhook URL with Telegram. Optionally sets the secret token.
set-telegram-bot-webhook:
    curl -fsSL -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook" \
        -H 'content-type: application/json' \
        -d "{\"url\": \"${TELEGRAM_HOOK_URL}\", \"secret_token\": \"${TELEGRAM_WEBHOOK_SECRET:-}\"}"
    @echo

# Fetch pending updates from Telegram (useful for debugging without a webhook).
get-updates-from-telegram-bot:
    curl -fsSL "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getUpdates"
    @echo

# Send a one-off plain message via the Bot API. Usage: just send-text-to-user 1234 "hello"
send-text-to-user user_id text:
    curl -fsSL -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
        -H 'content-type: application/json' \
        -d "{\"chat_id\": $1, \"text\": \"$2\"}"
    @echo

# Generate a fresh TOTP secret for a user and print the otpauth URI.
# Use the URI with any QR generator or paste into 1Password / Authy.
enroll-totp user:
    uv run python -m bot_cmder.cli enroll-totp --user {{user}}

# List every user with a stored TOTP enrollment.
list-totp:
    uv run python -m bot_cmder.cli list-totp

# Drop one user's TOTP enrollment.
revoke-totp user:
    uv run python -m bot_cmder.cli revoke-totp --user {{user}}

# Print a fresh BOT_CMDER_MASTER_KEY suitable for .env.
# WARNING: regenerating this invalidates every existing enrollment.
gen-master-key:
    @uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Phase 4 — push the slash command schema to Discord. Run after
# editing the registry (e.g. adding a yaml action) so Discord's
# autocomplete UI matches what the bot can actually handle. Pass
# `--guild=<id>` for dev (instant propagation, one server only);
# without args pushes globally (~1h propagation, every guild + DMs).
register-discord *args:
    uv run python -m scripts.register_discord_commands "$@"

# Phase 5 — generate Slack app manifest YAML from the live registry.
# Print to stdout by default; pipe to a file or use `--out file`.
# Then paste into your Slack app → Features → App Manifest tab → Save
# → accept the reinstall confirmation. URL is taken from
# SLACK_REQUEST_URL env, falling back to NGROK_DOMAIN — pass
# `--request-url <url>` to override per call.
register-slack *args:
    uv run python -m scripts.register_slack_commands "$@"
