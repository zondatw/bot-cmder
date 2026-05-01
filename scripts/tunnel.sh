#!/usr/bin/env bash
#
# Bring up a cloudflared quick tunnel pointed at the local bot, capture
# its (random, ephemeral) trycloudflare.com URL, write it back into
# .env as TELEGRAM_HOOK_URL, and (re-)register that URL with the
# Telegram Bot API.
#
# Stays in the foreground tailing the tunnel log; Ctrl-C cleans up.
#
# Required env (loaded by `just` from .env):
#   TELEGRAM_TOKEN
# Optional:
#   TELEGRAM_WEBHOOK_SECRET   sent to setWebhook so the bot can verify
#   BIND_PORT                 local port the bot listens on (default 47823)

set -euo pipefail

PORT="${BIND_PORT:-47823}"
WAIT_SECONDS=30

if [[ -z "${TELEGRAM_TOKEN:-}" ]]; then
    echo "ERROR: TELEGRAM_TOKEN is not set in .env" >&2
    exit 1
fi
if ! command -v cloudflared >/dev/null; then
    echo "ERROR: cloudflared not found. Install with: brew install cloudflared" >&2
    exit 1
fi

LOG=$(mktemp -t bot-cmder-tunnel.XXXXXX.log)
cleanup() {
    [[ -n "${CFL_PID:-}" ]] && kill "$CFL_PID" 2>/dev/null || true
    [[ -n "${TAIL_PID:-}" ]] && kill "$TAIL_PID" 2>/dev/null || true
    rm -f "$LOG"
}
trap cleanup EXIT INT TERM

echo ">>> starting cloudflared quick tunnel → http://localhost:${PORT}"
cloudflared tunnel --url "http://localhost:${PORT}" >"$LOG" 2>&1 &
CFL_PID=$!

echo ">>> waiting up to ${WAIT_SECONDS}s for tunnel URL..."
URL=""
for _ in $(seq 1 $((WAIT_SECONDS * 2))); do
    if ! kill -0 "$CFL_PID" 2>/dev/null; then
        echo "ERROR: cloudflared exited early. Last lines:" >&2
        tail -20 "$LOG" >&2
        exit 1
    fi
    URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)
    [[ -n "$URL" ]] && break
    sleep 0.5
done

if [[ -z "$URL" ]]; then
    echo "ERROR: tunnel URL not seen within ${WAIT_SECONDS}s. Log:" >&2
    cat "$LOG" >&2
    exit 1
fi

HOOK_URL="${URL}/webhooks/telegram"
echo ">>> tunnel up: ${URL}"
echo ">>> webhook URL: ${HOOK_URL}"

# Update .env in place (works on macOS sed and GNU sed).
if [[ -f .env ]]; then
    if grep -q '^TELEGRAM_HOOK_URL=' .env; then
        sed -i.bak "s|^TELEGRAM_HOOK_URL=.*|TELEGRAM_HOOK_URL=${HOOK_URL}|" .env
    else
        printf '\nTELEGRAM_HOOK_URL=%s\n' "${HOOK_URL}" >> .env
    fi
    rm -f .env.bak
    echo ">>> .env: TELEGRAM_HOOK_URL updated"
else
    echo "WARNING: .env not found, skipping .env update" >&2
fi

# Register with Telegram. Use python to build the JSON payload (the
# secret may contain shell-hostile characters) and to omit
# secret_token entirely when it is unset (Telegram requires 1-256
# chars; an empty string is rejected).
PAYLOAD=$(
    SECRET="${TELEGRAM_WEBHOOK_SECRET:-}" HOOK="${HOOK_URL}" python3 - <<'PY'
import json, os
payload = {"url": os.environ["HOOK"]}
secret = os.environ.get("SECRET", "")
if secret:
    payload["secret_token"] = secret
print(json.dumps(payload))
PY
)

# Retry loop — fresh trycloudflare subdomains take a few seconds to
# propagate to public DNS, and Telegram's resolver typically lags
# briefly behind ours. Six tries x 5s = 30s budget, which has been
# enough in practice.
echo ">>> registering webhook with Telegram (DNS may take a few seconds)..."
ATTEMPTS=6
for attempt in $(seq 1 $ATTEMPTS); do
    RESP=$(curl -sS -X POST \
        "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook" \
        -H 'content-type: application/json' \
        -d "${PAYLOAD}")
    if echo "$RESP" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("ok") else 1)' 2>/dev/null; then
        echo ">>> Telegram says: ${RESP}"
        break
    fi
    if [[ $attempt -eq $ATTEMPTS ]]; then
        echo "ERROR: setWebhook failed after ${ATTEMPTS} attempts. Last response:" >&2
        echo "$RESP" >&2
        exit 1
    fi
    echo ">>> attempt ${attempt}/${ATTEMPTS} failed, retrying in 5s: ${RESP}"
    sleep 5
done

echo
echo ">>> tunnel running. Ctrl-C to stop."
echo "    (cloudflared output streaming below)"
echo

# Stream cloudflared's continuing log to user's terminal and wait.
tail -f "$LOG" &
TAIL_PID=$!
wait "$CFL_PID"
