# Telegram Polling Mode

Run `bot-cmder` against Telegram **without a public HTTPS URL** —
no domain, no tunnel, no ngrok, no firewall ports. The bot dials
**outbound** to `api.telegram.org` and Telegram pushes updates back
through the long-poll connection.

> **Use it when**: you're on a home lab, behind NAT, in a corp
> network with restrictive ingress, or just want to avoid the
> tunnel maintenance dance.
>
> **Don't use it when**: you're running multiple bot instances against
> the same `TELEGRAM_TOKEN` (they'd race for updates — Telegram
> delivers each update once to whoever's polling first). Webhook
> mode behind a load balancer is the right answer for HA.

---

## How to switch

```shell
# .env
TELEGRAM_MODE=polling
# Optional tuning (defaults shown):
TELEGRAM_POLLING_TIMEOUT_S=25            # long-poll wait, max 50
TELEGRAM_POLLING_DROP_PENDING=false      # discard webhook backlog at startup
```

Restart `just dev` and you should see:

```
INFO  bot_cmder: telegram adapter mounted (mode=polling)
INFO  bot_cmder.adapters.telegram.daemon: telegram daemon polling (timeout=25s, drop_pending=False)
```

That's it — open Telegram, type `/help`. Updates arrive via the
existing dispatcher / OTP gate / audit log unchanged.

---

## How it works

### Mutex with webhook (auto-deleted at startup)

Telegram's API does not allow both modes at once: `getUpdates` returns
HTTP 409 whenever a webhook URL is registered. To make `TELEGRAM_MODE`
flipping painless, the daemon does this at startup:

1. Calls `getWebhookInfo`. If a webhook URL is set, logs a WARNING
   showing what was configured.
2. Calls `deleteWebhook(drop_pending_updates=...)`. The webhook is now gone.
3. Begins long-polling.

If you want to switch **back** to webhook mode, run
`just set-telegram-bot-webhook` (no need to manually clear polling
state — it lives in the daemon's memory and dies when the process
does).

### Long-poll loop

```
┌──────────────────────────────────────────────────────────────┐
│  daemon.run() — runs forever inside FastAPI lifespan         │
│                                                              │
│  while True:                                                 │
│      updates = await client.get_updates(                     │
│          offset=last_seen_id + 1,                            │
│          timeout_s=25,                                       │
│          allowed_updates=["message"],                        │
│      )                                                       │
│      for update in updates:                                  │
│          msg = adapter.parse(update)                         │
│          await dispatcher.dispatch(msg) → adapter.send(...)  │
│      last_seen_id = max(u.update_id for u in updates)        │
└──────────────────────────────────────────────────────────────┘
```

- **`timeout=25s`** — Telegram holds the connection open up to 25s
  waiting for a new update. No new updates → empty list returned →
  immediate next poll. New update arrives mid-wait → connection
  closes immediately and we process. Reduces request count vs.
  short polling without leaving sockets idle long enough for
  middleboxes to drop them.
- **`offset=last_seen_id + 1`** — acks every prior update so it's
  never re-delivered. Kept in **memory only** for MVP; a bot
  restart re-reads up to ~24h of pending updates (Telegram's
  default retention). Acceptable for SRE bot since:
  - SAFE diagnostic commands (`/help`, `/health`, `/service status`)
    are read-only and idempotent
  - PRIVILEGED commands gated by OTP — replays would just hit
    "no pending privileged command" / "session expired" and audit
    that
- **`allowed_updates=["message"]`** — narrow filter so the loop
  doesn't wake on edited messages, callback queries, channel posts,
  etc. that the dispatcher would ignore anyway. Saves bandwidth and
  CPU.

### Backoff

Network errors / 5xx → exponential backoff capped at 30 s.

| Failure | Action | After |
|---|---|---|
| `httpx.ConnectError` / `httpx.ReadTimeout` | sleep 1, 2, 4, 8, 16, 30, 30… | reset to 1s on first successful poll |
| 5xx from Telegram | same as above | same |
| 409 Conflict (a webhook got re-registered while we were running) | log ERROR, exit daemon loop | manual fix required — operator must decide which mode |
| `asyncio.CancelledError` (FastAPI lifespan shutdown) | unwind cleanly, no retry | — |

The 409 case is intentionally fatal because it almost always
indicates a config conflict (e.g. you ran `just set-telegram-bot-webhook`
in a second terminal). Silently re-deleting the webhook every loop
would mask the bug; failing loud forces the operator to pick a side.

---

## When to prefer webhook

| | Polling | Webhook |
|---|---|---|
| Public HTTPS URL needed? | ❌ No | ✅ Yes |
| Tunnel needed (ngrok / cloudflared)? | ❌ No | ✅ Yes (or real domain) |
| Latency: command → bot sees it | up to ~25s (the long-poll wait) | typically < 100ms |
| Multi-instance HA? | ❌ One instance only (race for updates) | ✅ Behind a load balancer |
| Bandwidth (idle bot) | ~1 request / 25s | 0 requests |
| Default | — | ✅ |

For **dev / home lab / single-instance prod**: polling is fine and
removes the tunnel maintenance friction. For **production with HA
requirements**: webhook + load balancer.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot doesn't respond, log shows nothing | not actually in polling mode (typo `TELEGRAM_MODE=poling`) | startup now fail-fasts on invalid mode; check the `ValueError` in stderr |
| `409 Conflict` ERROR + daemon exits | webhook got re-registered (probably `just set-telegram-bot-webhook` ran somewhere) | pick one: re-run `just dev` (polling will re-claim) OR keep webhook + remove `TELEGRAM_MODE=polling` from `.env` |
| Replies of yesterday's commands flood when you start | Telegram queued them while a webhook was active | set `TELEGRAM_POLLING_DROP_PENDING=true` once, restart, then flip it back to false |
| First reply takes 25 seconds | normal — that's the long-poll wait. Subsequent commands arrive instantly because the next `getUpdates` cycle starts immediately | accepted trade-off; lower `TELEGRAM_POLLING_TIMEOUT_S` if you want faster cold start at the cost of more requests |
| `OSError: [Errno 65] No route to host` repeatedly | network down, or Telegram blocked by your egress filter | check connectivity: `curl https://api.telegram.org/bot<TOKEN>/getMe` |
