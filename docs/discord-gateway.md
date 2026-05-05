# Discord Gateway Mode

Run `bot-cmder` against Discord **without a public HTTPS URL** —
no domain, no tunnel, no firewall ports. The bot opens a WebSocket
**outbound** to Discord and chat events arrive over that connection.

> **Use it when**: home lab / NAT / restrictive corp egress, AND you
> can accept the UX trade-off below.
>
> **Don't use it when**: you have a public URL (use Phase 4 / Phase 5
> mode `interactions` instead — slash command UX is materially better).

---

## ⚠️ The big trade-off — slash commands stop working

**Discord Gateway does NOT deliver slash command interactions.**
This is a documented Discord platform limitation, not a missing
feature in this bot. Slash commands are a separate Discord product
(Application Commands) and ALWAYS go through HTTP Interactions, even
when a Gateway connection is open.

So in `DISCORD_MODE=gateway` you lose:

| Lost in Gateway mode | Was available in Interactions mode |
|---|---|
| `/help` autocomplete in Discord input | ✅ |
| Typed args (`code` STRING option for `/otp`) | ✅ |
| Per-command argument hints | ✅ |
| Ephemeral replies (only invoker sees) | ✅ |
| Auto-redaction in Discord's UI | ✅ |

The replacement UX is plain chat text:

| In a guild channel | In a DM with the bot |
|---|---|
| `@sre_bot help` | `help` |
| `@sre_bot service restart hello --host gce` | `service restart hello --host gce` |
| `@sre_bot otp 123456` | `otp 123456` |

The adapter normalizes both to the canonical `/cmd args` text the
dispatcher expects, so behavior downstream (ACL, OTP gate, audit) is
**byte-equivalent** to Interactions mode. Only the in-Discord
typing UX changes.

---

## Setup

### 1. Privileged intent — MUST enable first

Discord requires you to opt-in to the **Message Content Intent**
before any `MESSAGE_CREATE` event will arrive with non-empty
`content`. Without this step the bot connects fine, sees messages,
but reads empty strings and dispatches nothing.

1. https://discord.com/developers/applications → your app
2. Left sidebar → **Bot**
3. Scroll to **Privileged Gateway Intents**
4. Toggle **Message Content Intent** on
5. Save changes

> **Restriction at scale**: Discord requires verified-bot status if
> your bot is in 100+ guilds. For SRE-bot use cases you're well
> below that threshold; no verification needed.

### 2. Bot env

```shell
echo "DISCORD_MODE=gateway"      >> .env
# DISCORD_BOT_TOKEN was already set during Phase 4 setup.
# DISCORD_PUBLIC_KEY is irrelevant in gateway mode (no HMAC verify
# happens — frames arrive over an authenticated WSS); leave whatever
# you had, it's harmless.
```

### 3. Restart `just dev`

Expected startup log:

```
INFO  bot_cmder: discord adapter mounted (mode=gateway, MESSAGE_CONTENT intent must be enabled in dev portal)
INFO  bot_cmder.adapters.discord.gateway: discord gateway: connecting to wss://gateway.discord.gg/?v=10&encoding=json
INFO  bot_cmder.adapters.discord.gateway: discord gateway: identifying (intents=37376)
INFO  bot_cmder.adapters.discord.gateway: discord gateway: READY (bot_id=..., session=...)
```

`READY` confirms the WebSocket is alive and Discord acknowledged
our IDENTIFY. From this point on every `@sre_bot` mention or DM
arrives over the socket — **you can kill any tunnel** without
affecting Discord delivery.

---

## How it works

### Protocol

Discord Gateway is a stateful WebSocket protocol with sequence
numbers and explicit RESUME semantics:

```
┌──────────────────────────────────────────────────────────────┐
│  daemon.run() — runs forever inside FastAPI lifespan         │
│                                                              │
│  while True:                                                 │
│      url = resume_url if session_id else default_url         │
│      async with websockets.connect(url) as ws:               │
│          recv HELLO  → start heartbeat task                  │
│          send IDENTIFY (or RESUME if session exists)         │
│          recv READY  → save session_id + resume_url          │
│          for each event:                                     │
│              if MESSAGE_CREATE:                              │
│                  parse + dispatch (in separate task)         │
│              if HEARTBEAT_ACK:                               │
│                  reset unacked counter                       │
│              if RECONNECT (op 7):                            │
│                  exit stream, outer loop reconnects (RESUME) │
│              if INVALID_SESSION (op 9, payload=False):       │
│                  wipe state, fresh IDENTIFY next iteration   │
└──────────────────────────────────────────────────────────────┘
```

Key contracts:
- **HEARTBEAT every `interval_ms`** (~41s in production). Discord
  replies with HEARTBEAT_ACK; if 2 in a row go unacked the
  connection is "zombied" and the daemon force-closes for a
  RESUME-eligible reconnect.
- **Sequence number tracking** for RESUME — Discord replays missed
  events from the last `seq` we received.
- **Dispatch in a separate task** so a slow handler (SSH 30s) can't
  block the WS read loop and stop heartbeats.

### Backoff

| Failure | Action | Recovery |
|---|---|---|
| `httpx.ConnectError` / `OSError` | sleep 1, 2, 4, 8, 16, 30, 30… | reset to 1s on first successful WSS connect |
| `WebSocketException` | same | same |
| `RECONNECT` (op 7) from server | exit stream, immediately reconnect | RESUME (no backoff) |
| `INVALID_SESSION` resumable=True | exit stream, immediately reconnect | RESUME |
| `INVALID_SESSION` resumable=False | wipe state, sleep 2s, fresh IDENTIFY | docs say wait 1-5s; we use 2s |
| `asyncio.CancelledError` (FastAPI shutdown) | unwind cleanly, stop heartbeat task | — |

The fatal-close handling (close codes 4010+) wipes state too — those
codes mean Discord rejected our IDENTIFY (bad token, missing
intent, etc.) and a RESUME would just fail the same way.

---

## When to prefer Interactions mode

| | Gateway | Interactions |
|---|---|---|
| Public HTTPS URL needed? | ❌ No | ✅ Yes |
| Tunnel needed (ngrok / cloudflared)? | ❌ No | ✅ Yes |
| Slash command UX (autocomplete + typed args) | ❌ No | ✅ Yes |
| Ephemeral replies | ❌ No | ✅ Yes |
| `@-mention` syntax required (in guilds) | ✅ Yes | ❌ No |
| MESSAGE_CONTENT privileged intent required | ✅ Yes | ❌ No |
| Multi-instance HA | ❌ One instance per shard | ✅ Behind a load balancer |
| Latency: command → bot sees it | < 100ms (push) | < 100ms (push) |
| Default | — | ✅ |

**Rule of thumb**: if you have a public URL, use `interactions` —
the slash command UX is meaningfully better. Use `gateway` only when
the public URL is the actual blocker.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Daemon connects (`READY`) but `@sre_bot help` does nothing | **MESSAGE_CONTENT privileged intent NOT enabled in dev portal** — bot reads empty `content` strings | dev portal → Bot → Privileged Gateway Intents → enable Message Content Intent |
| `4014` close code in dev log + daemon exits | Privileged intent claimed in IDENTIFY but not enabled in app config | same as above |
| `4004` close code | bad bot token | `.env` SLACK_BOT_TOKEN — check value matches Bot → Reset Token |
| `4013` close code | invalid intent value | shouldn't happen with our hardcoded bitmask; report a bug |
| Bot echoes its own message into a loop | reply-loop guard regression (the `author.bot` check) | check `tests/adapters/discord/test_adapter.py::test_message_create_from_bot_returns_none` is still in the suite |
| In a guild, bot ignores `@sre_bot help` (no log) | bot wasn't @-mentioned correctly (Discord renders it as raw text on copy-paste) | use Discord's autocomplete: type `@` → pick the bot from the dropdown |
| `/cmd` typed in Discord shows "didn't respond" | slash commands don't work in Gateway mode (by design) — use `@bot cmd` instead | accept the trade-off, or switch to `DISCORD_MODE=interactions` |
| Daemon reconnects every ~30 min | normal — Discord rebalances capacity. RESUME path replays missed events | no action |
