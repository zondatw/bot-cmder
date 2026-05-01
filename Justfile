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
# the tunnel running. Re-run after every cloudflared restart.
tunnel:
    bash scripts/tunnel.sh

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

# Echo the env settings used by the bot.
show-env-settings:
    @echo "--- .env settings ---"
    @echo "TELEGRAM_TOKEN: ${TELEGRAM_TOKEN:-<unset>}"
    @echo "TELEGRAM_WEBHOOK_SECRET: ${TELEGRAM_WEBHOOK_SECRET:+<set>}"
    @echo "TELEGRAM_HOOK_URL: ${TELEGRAM_HOOK_URL:-<unset>}"
    @echo "APP_CONFIG_PATH: ${APP_CONFIG_PATH:-./config/app.yaml}"

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
