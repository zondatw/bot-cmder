#!/usr/bin/env bash
#
# Bring up an ngrok tunnel on your reserved static domain, point
# TELEGRAM_HOOK_URL at it in .env, and (re-)register the webhook
# with Telegram. Stays in the foreground; Ctrl-C tears down.
#
# One-time setup before this script works:
#   1. Sign up at https://ngrok.com (free tier is fine).
#   2. brew install ngrok
#   3. ngrok config add-authtoken <token from dashboard>
#   4. Reserve a free static domain at
#      https://dashboard.ngrok.com/domains
#   5. Add NGROK_DOMAIN=<that-domain>.ngrok-free.app to .env
#
# Required env (loaded by `just` from .env):
#   TELEGRAM_TOKEN, NGROK_DOMAIN
# Optional:
#   TELEGRAM_WEBHOOK_SECRET   sent to setWebhook so the bot can verify
#   BIND_PORT                 local port the bot listens on (default 47823)
#
# Why ngrok and not cloudflared trycloudflare:
#   trycloudflare hands out a fresh random subdomain on every restart;
#   that subdomain takes 30-90s to propagate to public DNS, and any
#   resolver that caches the inevitable initial NXDOMAIN becomes
#   stuck for the full negative-cache TTL. ngrok's reserved domain
#   exists permanently, so DNS is always warm.

set -euo pipefail

PORT="${BIND_PORT:-47823}"

if [[ -z "${TELEGRAM_TOKEN:-}" ]]; then
    echo "ERROR: TELEGRAM_TOKEN is not set in .env" >&2
    exit 1
fi
if [[ -z "${NGROK_DOMAIN:-}" ]]; then
    cat >&2 <<'MSG'
ERROR: NGROK_DOMAIN not set in .env

One-time setup:
  1. brew install ngrok
  2. Sign up at https://ngrok.com
  3. ngrok config add-authtoken <token>
  4. Reserve a domain at https://dashboard.ngrok.com/domains
  5. Add to .env:  NGROK_DOMAIN=<your-domain>.ngrok-free.app
MSG
    exit 1
fi
if ! command -v ngrok >/dev/null; then
    echo "ERROR: ngrok not found. Install with: brew install ngrok" >&2
    exit 1
fi

URL="https://${NGROK_DOMAIN}"
HOOK_URL="${URL}/webhooks/telegram"

# Update .env in place so `just set-telegram-bot-webhook` and
# `just hook-status` see the same URL we're about to register.
if [[ -f .env ]]; then
    if grep -q '^TELEGRAM_HOOK_URL=' .env; then
        sed -i.bak "s|^TELEGRAM_HOOK_URL=.*|TELEGRAM_HOOK_URL=${HOOK_URL}|" .env
    else
        printf '\nTELEGRAM_HOOK_URL=%s\n' "${HOOK_URL}" >> .env
    fi
    rm -f .env.bak
    echo ">>> .env: TELEGRAM_HOOK_URL = ${HOOK_URL}"
fi

LOG=$(mktemp -t bot-cmder-ngrok.XXXXXX.log)
cleanup() {
    [[ -n "${NG_PID:-}" ]] && kill "$NG_PID" 2>/dev/null || true
    [[ -n "${TAIL_PID:-}" ]] && kill "$TAIL_PID" 2>/dev/null || true
    rm -f "$LOG"
}
trap cleanup EXIT INT TERM

echo ">>> starting ngrok → ${URL} (forwarding to http://localhost:${PORT})"
ngrok http --domain="${NGROK_DOMAIN}" --log=stdout "${PORT}" >"$LOG" 2>&1 &
NG_PID=$!

# Wait for ngrok to actually be serving.
echo ">>> waiting for tunnel..."
LOCAL_OK=0
for _ in $(seq 1 30); do
    if ! kill -0 "$NG_PID" 2>/dev/null; then
        echo "ERROR: ngrok exited early. Last log lines:" >&2
        tail -20 "$LOG" >&2
        exit 1
    fi
    HTTP_CODE=$(curl -sS -o /dev/null -m 3 -w "%{http_code}" "${URL}/healthz" 2>/dev/null) || HTTP_CODE="000"
    if [[ "$HTTP_CODE" != "000" ]]; then
        echo ">>> tunnel reachable (HTTP ${HTTP_CODE} on /healthz)"
        LOCAL_OK=1
        break
    fi
    sleep 1
done
if [[ $LOCAL_OK -eq 0 ]]; then
    echo "WARNING: tunnel didn't respond in 30s. Last ngrok lines:" >&2
    tail -20 "$LOG" >&2
fi

# setWebhook payload — omit secret_token if empty (Telegram requires 1-256 chars).
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

# With ngrok's stable domain, DNS is permanent — setWebhook usually
# succeeds first try. Three attempts is enough cushion for transient
# network blips.
echo ">>> registering webhook with Telegram..."
ATTEMPTS=3
WEBHOOK_OK=0
for attempt in $(seq 1 $ATTEMPTS); do
    RESP=$(curl -sS -X POST \
        "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook" \
        -H 'content-type: application/json' \
        -d "${PAYLOAD}")
    if echo "$RESP" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("ok") else 1)' 2>/dev/null; then
        echo ">>> Telegram says: ${RESP}"
        WEBHOOK_OK=1
        break
    fi
    echo ">>> attempt ${attempt}/${ATTEMPTS} failed, retrying in 5s: ${RESP}"
    sleep 5
done

if [[ $WEBHOOK_OK -eq 0 ]]; then
    echo >&2
    echo "WARNING: setWebhook didn't succeed. Tunnel still up — retry:" >&2
    echo "    just set-telegram-bot-webhook" >&2
fi

echo
echo ">>> tunnel running. Ctrl-C to stop."
echo "    ngrok web inspector:  http://localhost:4040"
echo

tail -f "$LOG" &
TAIL_PID=$!
wait "$NG_PID"
