# Slack Socket Mode

Run `bot-cmder` against Slack **without a public HTTPS URL** — no
domain, no tunnel, no firewall ports. The bot opens a WebSocket
**outbound** to Slack and slash commands are pushed back through
that connection. Same dispatcher, same OTP gate, same audit log,
same `/cmd` UX as the Events API path; only ingestion differs.

> **Use it when**: home lab, NAT, restrictive corp egress, or you
> just want to skip tunnel maintenance.
>
> **Don't use it when**: you need to handle public Events API
> webhooks for non-slash-command flows (e.g. `app_mention` events
> for assistant-style features). Phase 6b's Socket Mode handles
> slash commands only — same surface as Phase 5.

---

## Setup

### 1. Slack app side — enable Socket Mode + generate app token

In your Slack app config (https://api.slack.com/apps → your app):

1. **Basic Information** → scroll to **App-Level Tokens** →
   **Generate Token and Scopes**:
   - Name: `socket-mode` (anything)
   - Add scope: `connections:write`
   - Generate → copy the `xapp-...` token (shown once)

2. **Socket Mode** (left sidebar) → toggle **Enable Socket Mode** on.

   ✨ Side effect: enabling Socket Mode greys out the Events API
   "Request URL" field — Slack enforces the mutex, so you can't
   accidentally have both modes pointing at the same app.

3. **🚨 Reinstall the app to your workspace.** Left sidebar →
   **Install App** → **Reinstall to Workspace** → Allow.

   This is the #1 dogfood gotcha: Slack's slash command routing
   is decided at install time and does NOT auto-switch to Socket
   Mode just because you toggled it on. Without a reinstall the
   daemon will connect cleanly (`hello (connections=1)` log line)
   but slash commands keep being delivered to the OLD path
   (events webhook), so your bot sees nothing in `dev` log when
   you type `/help` and Slack eventually shows
   `應用程式未回應` / "application didn't respond".

   The xapp- token doesn't change on reinstall, so no need to
   restart `just dev` — the existing socket connection picks up
   slash commands as soon as the routing table updates.

4. Slash commands stay registered as before (the manifest from
   `just register-slack` is mode-independent — Socket Mode just
   changes the delivery channel).

### 2. Bot side — set env

```shell
echo "SLACK_MODE=socket"           >> .env
echo "SLACK_APP_TOKEN=xapp-..."    >> .env
# SLACK_SIGNING_SECRET is irrelevant in socket mode (no HMAC verify
# happens — events come over an authenticated WSS, not via HTTP);
# leave whatever value you had, it's harmless.
```

### 3. Restart `just dev`

Expected startup log:

```
INFO  bot_cmder: slack adapter mounted (mode=socket, reply_visibility=by_risk, 0 override(s))
INFO  bot_cmder.adapters.slack.socket: slack socket: connecting to wss URL
INFO  bot_cmder.adapters.slack.socket: slack socket: hello (connections=1)
```

`hello (connections=1)` confirms the WebSocket is alive. From this
point on every `/cmd` typed in Slack arrives over the socket —
**you can kill any tunnel** (ngrok / cloudflared) without affecting
Slack delivery.

---

## How it works

```
┌─────────────────────────────────────────────────────────────┐
│  daemon.run() — runs forever inside FastAPI lifespan        │
│                                                             │
│  while True:                                                │
│      wss_url = await fetch_url(SLACK_APP_TOKEN)             │
│        ↳ POST apps.connections.open → returns WSS URL       │
│          (one-time, ~3 min validity)                        │
│      async with websockets.connect(wss_url) as ws:          │
│          async for raw in ws:                               │
│              event = json.loads(raw)                        │
│              if event.type == "hello": pass                 │
│              if event.type == "disconnect": break (reconn)  │
│              if event.envelope_id:                          │
│                  await ws.send({envelope_id})  # ACK <3s    │
│              if event.type == "slash_commands":             │
│                  msg = adapter.parse(event.payload)         │
│                  asyncio.create_task(dispatch_and_send(msg))│
└─────────────────────────────────────────────────────────────┘
```

Key contracts:
- **ACK within 3 seconds** of receiving an envelope — else Slack
  treats the request as unhandled and may retry. Done BEFORE
  dispatch so a slow handler (SSH 30s) can't make us miss it.
- **Dispatch in a separate task** so a slow handler can't block
  the WS read loop and back up subsequent events.
- **Reconnect on `disconnect` event** — Slack sends this for graceful
  capacity rebalancing every ~30 minutes. Outer loop fetches a fresh
  WSS URL and reconnects automatically.

### Backoff

| Failure | Action | Recovery |
|---|---|---|
| `httpx.ConnectError` / `httpx.ReadTimeout` | sleep 1, 2, 4, 8, 16, 30, 30… | reset to 1s on first successful connect |
| 5xx from `apps.connections.open` | same | same |
| 401/403 from `apps.connections.open` | log ERROR, exit daemon loop | manual fix — operator rotates `SLACK_APP_TOKEN` |
| `websockets.ConnectionClosed` | reconnect (treated as disconnect) | next iteration of outer loop |
| `asyncio.CancelledError` (FastAPI shutdown) | unwind cleanly | — |

The 401/403 case is intentionally fatal — bad app token can't be
fixed by retrying. Operator must rotate, not the daemon.

---

## When to prefer events mode

| | Socket | Events |
|---|---|---|
| Public HTTPS URL needed? | ❌ No | ✅ Yes |
| Tunnel needed (ngrok / cloudflared)? | ❌ No | ✅ Yes (or real domain) |
| Latency: slash command → bot sees it | < 100ms (push) | < 100ms (push) |
| Multi-instance HA? | ❌ One instance only (each socket is one consumer) | ✅ Behind a load balancer |
| Idle bandwidth | ~zero (open WSS, no traffic) | zero |
| Slack app config | Socket Mode toggle ON, Events URL greyed out | Events URL set, Socket Mode off |
| Default | — | ✅ |

For dev / home lab / single-instance prod: socket is fine. For
production with HA: events + load balancer.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `slack adapter disabled` log even with SLACK_MODE=socket | SLACK_APP_TOKEN missing | set the env var |
| `401 from apps.connections.open` log + daemon exits | bad app token / missing `connections:write` scope | regenerate token in Slack app config; also confirm scope is `connections:write` |
| Socket connects (`hello`) but slash commands still don't arrive | Slack app's Socket Mode toggle is OFF | enable in app config — the env mode + app config must match |
| Socket connects (`hello`) AND Socket Mode is on AND nothing arrives + Slack shows `應用程式未回應` / "application didn't respond" | **app wasn't reinstalled after enabling Socket Mode** — slash command routing is install-time-decided | Install App → Reinstall to Workspace → Allow. xapp- token unchanged so no `just dev` restart needed; routing updates server-side and the existing socket starts receiving slash commands within seconds. See [§Setup step 3](#1-slack-app-side--enable-socket-mode--generate-app-token). |
| `no log line for /cmd` invocations | bot user not invited to the channel where `/cmd` was typed | `/invite @your-bot` in that channel; or use a DM with the bot |
| `Apologies — there was a problem` reply in Slack | bot crashed mid-dispatch | check `just dev` stderr; usually an SSH connect / config issue |
| Daemon reconnects every ~30 min ("graceful disconnect") | normal — Slack rebalances capacity | no action; `/cmd` invocations during reconnect briefly delayed |
