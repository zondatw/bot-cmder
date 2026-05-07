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
    uv run bot-cmder serve --reload

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

# Phase 7 — scan for committed personal identifiers (Telegram /
# Discord / Slack user IDs, ngrok hostnames, etc.) using gitleaks.
# Same config (.gitleaks.toml) the pre-commit hook + CI use, so
# `just check-leaks` exactly mirrors what would block your PR.
# Scans the full git history; for working-tree-only use the
# pre-commit hook (it auto-runs on commit).
check-leaks:
    gitleaks detect --no-banner --verbose

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

# Issue #20 — first-run scaffold. Creates app.yaml + .env (with a
# freshly generated BOT_CMDER_MASTER_KEY) into ./config/, plus
# ./var/. Use `bot-cmder init --config-dir .` directly for the
# same effect when not using just.
init:
    uv run bot-cmder init --config-dir .

# Generate a fresh TOTP secret for a user and print the otpauth URI.
# Use the URI with any QR generator or paste into 1Password / Authy.
enroll-totp user:
    uv run bot-cmder enroll-totp --user {{user}}

# List every user with a stored TOTP enrollment.
list-totp:
    uv run bot-cmder list-totp

# Drop one user's TOTP enrollment.
revoke-totp user:
    uv run bot-cmder revoke-totp --user {{user}}

# Print a fresh BOT_CMDER_MASTER_KEY suitable for .env.
# WARNING: regenerating this invalidates every existing enrollment.
gen-master-key:
    @uv run bot-cmder gen-master-key

# Phase 4 — push the slash command schema to Discord. Run after
# editing the registry (e.g. adding a yaml action) so Discord's
# autocomplete UI matches what the bot can actually handle. Pass
# `--guild=<id>` for dev (instant propagation, one server only);
# without args pushes globally (~1h propagation, every guild + DMs).
register-discord *args:
    uv run bot-cmder discord-register "$@"

# Phase 5 — generate Slack app manifest YAML from the live registry.
# Print to stdout by default; pipe to a file or use `--out file`.
# Then paste into your Slack app → Features → App Manifest tab → Save
# → accept the reinstall confirmation. URL is taken from
# SLACK_REQUEST_URL env, falling back to NGROK_DOMAIN — pass
# `--request-url <url>` to override per call.
register-slack *args:
    uv run bot-cmder slack-manifest "$@"

# Phase 9 — Docker image build (native arch only, fast). For multi-arch
# use `docker buildx build --platform linux/amd64,linux/arm64 .` — used by
# the release workflow, not typically needed for local dev.
docker-build:
    docker build -t bot-cmder:local .

# Run the locally-built image with config + state mounted out of the
# repo's ./config/ + ./var/. Convenient for "does the wheel-install
# path of the bot actually serve?" loops.
docker-run:
    docker run --rm -it \
        -v "$(pwd)/config:/etc/bot-cmder:ro" \
        -v "$(pwd)/var:/var/lib/bot-cmder" \
        -p 47823:47823 \
        bot-cmder:local

# Drop into a shell inside the image — useful for poking around the
# venv layout or verifying mounted files are readable as the
# `botcmder` user (UID 1000).
docker-shell:
    docker run --rm -it --entrypoint sh bot-cmder:local
