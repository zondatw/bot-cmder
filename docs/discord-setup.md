# Discord Setup

End-to-end walkthrough for wiring `bot-cmder` into Discord. Every value
you need can be found inside the Discord developer portal; this doc
points at exactly where.

> **Prerequisite**: a Discord account, a server you have **Manage
> Server** on (a personal test server is fine — see step 3), and the
> bot already runs locally with `just dev` reachable via a public
> tunnel (`just tunnel-ngrok`).

---

## 1. Create the Discord application

1. Go to <https://discord.com/developers/applications>.
2. Click **"New Application"** (top-right). Pick a name; this is the
   bot's display name.
3. After it lands on the application page, the left sidebar has the
   five sections we'll touch: **General Information**, **OAuth2**,
   **Bot**, plus the top-row "Interactions Endpoint URL" textbox
   (which is on the General Information page).

---

## 2. Pull the four environment variables

| `.env` key | Where in the dev portal | Secret? | Required? |
|---|---|---|---|
| `DISCORD_APPLICATION_ID` | **General Information** → "Application ID" (or the URL of the app page) | no | ✅ |
| `DISCORD_PUBLIC_KEY` | **General Information** → "Public Key" (64-char hex) | no | ✅ |
| `DISCORD_BOT_TOKEN` | **Bot** → "Reset Token" (see below) | **yes** | ✅ |
| `DISCORD_GUILD_ID` | Discord client (not dev portal) — see step 3 | no | optional, but recommended for dev |

### 2a. `DISCORD_APPLICATION_ID` and `DISCORD_PUBLIC_KEY`

Both live on **General Information**. Copy them straight into `.env`:

```shell
echo "DISCORD_APPLICATION_ID=1500111111111111111" >> .env
echo "DISCORD_PUBLIC_KEY=00ab3f2fa642099db678327bde38b7bb354bfdd34cbf305826f2aad0d8f66229" >> .env
```

Neither is a secret — `APPLICATION_ID` is part of every URL Discord
generates for the bot, and `PUBLIC_KEY` is a verify key (the matching
secret signing key never leaves Discord's servers).

### 2b. `DISCORD_BOT_TOKEN`

1. Left sidebar → **Bot**.
2. If this is the very first time, click **"Add Bot"** then confirm.
3. Under "Token", click **"Reset Token"** (you'll be asked to confirm,
   and possibly re-enter your 2FA).
4. Discord shows the new token **once**. Click **"Copy"**.
5. Paste into `.env` immediately:

   ```shell
   echo "DISCORD_BOT_TOKEN=MTUwMDI3OTk5MTE5MDAzMjQzNQ.GeyUSf.xxxxx" >> .env
   ```

> ⚠️ **The token is a password.** Treat it accordingly:
> - Never commit `.env` (already gitignored).
> - Never paste the token into a chat, issue, or screenshot. (If you
>   do — see "Rotating leaked secrets" at the bottom.)
> - Resetting the token instantly invalidates the previous one. Any
>   running bot using the old token will go offline; you'll need to
>   restart it after updating `.env`.

### 2c. `DISCORD_GUILD_ID` (optional, recommended for dev)

Only relevant for `just register-discord` — it scopes slash command
registration to one server so updates propagate **instantly** instead
of taking ~1 hour. Without it, commands push globally.

To get the value:

1. In the Discord client (not the dev portal): **Settings** (gear) →
   **Advanced** → enable **"Developer Mode"**.
2. Right-click the server icon in the left sidebar → **"Copy Server
   ID"**.
3. Paste into `.env`:

   ```shell
   echo "DISCORD_GUILD_ID=945123456789012345" >> .env
   ```

| | Set `DISCORD_GUILD_ID` | Unset |
|---|---|---|
| Visible in | only that server | every server the bot is in + DMs |
| Time to propagate after `register-discord` | **instant** | ~1 hour |
| Recommended for | dev / local testing | production |

---

## 3. Invite the bot to a server (OAuth2)

A Discord bot must belong to at least one server. For development, the
easiest path is a personal test server you control.

### 3a. Create a personal test server (skip if you already have one)

In the Discord client: left-rail **"+"** icon → **"Create My Own"** →
"For me and my friends" → name it whatever (e.g. `bot-cmder-test`).

### 3b. Build the OAuth2 invite URL

1. Dev portal → left sidebar → **OAuth2** → **URL Generator**.
2. Under "Scopes", check:
   - ✅ `bot`
   - ✅ `applications.commands`
3. Under "Bot Permissions", you can leave everything unchecked — the
   bot replies to slash commands via the per-interaction webhook URL,
   which doesn't require any guild permissions. (Optional safety:
   check `Send Messages` and `Read Message History` for flexibility,
   though they're not used in the Phase 4 flow.)
4. Scroll to the bottom — copy the generated URL. It looks like:

   ```
   https://discord.com/api/oauth2/authorize?client_id=<APP_ID>&permissions=0&scope=bot+applications.commands
   ```

### 3c. Authorize

1. Paste the URL into your browser.
2. Pick the target server from the dropdown.
3. Click **"Authorize"** and pass the captcha.
4. Confirm in Discord — the bot now appears in the server's member
   list (offline status is normal: we use HTTP Interactions, not the
   Gateway WebSocket).

---

## 4. Set the Interactions Endpoint URL

This is what makes Discord post to your bot.

1. Make sure `just dev` is running and your tunnel is up. If you set
   up Telegram earlier with `just tunnel-ngrok`, the same tunnel works
   here — just append `/webhooks/discord` to the public URL.

   ```
   https://<your-ngrok-domain>.ngrok-free.app/webhooks/discord
   ```

2. Dev portal → **General Information** → "Interactions Endpoint URL"
   textbox.
3. Paste the URL → **Save Changes**.

Discord immediately probes the endpoint with a signed PING. If signing
verification fails, the page refuses to save with a red error. When it
saves successfully, you've proven both:

- the tunnel routes correctly,
- `DISCORD_PUBLIC_KEY` matches the application's actual key,
- the bot is running and answering PING.

---

## 5. Register the slash command schema

```shell
just register-discord
```

Builds the manifest from the live registry (every top-level Command +
Router) and PUTs it to Discord. Output:

```
Manifest: 8 top-level command(s):
  /health + args — HTTP healthcheck against configured targets
  /help — Show available commands
  /kubectl + args — Run a whitelisted kubectl subcommand ...
  /otp + code — Submit OTP code ...
  /runbook + args — List and run pre-deployed shell scripts in runbook.dir
  /service + args — Run predefined ops actions against configured services
  /ssh + args — Run a remote command on a configured host (allowlist + TOTP)
  /whoami — Show your normalized identity and role

Pushing to guild 945123456789012345 (instant propagation)...
Discord accepted 8 command(s).
```

Re-run any time you add a new yaml action (`/service <new_action>`),
add/remove a builtin, or change a description.

> The slash command schema is intentionally flat: each command takes
> one optional STRING `args` field (or required `code` for `/otp`).
> The DiscordAdapter recombines `/<cmd> <args>` into the same
> text-shaped message Telegram produces, so the dispatcher (router
> rewrite, ACL, OTP gate, audit) handles both platforms identically.

---

## 6. Verify in chat

In the test server (or in a DM with the bot, if you registered
globally):

```
/help                                            # 8-command listing
/whoami                                          # confirms your discord:<id>
/service args:"info hello"                       # service detail dump
/service args:"sysinfo hello"                    # SAFE action, fan-out
/service args:"restart hello --host gce"         # PRIVILEGED → /otp prompt
/otp code:"123456"                               # → SSH executes
```

Behind the scenes:

```
1. /service args:"restart hello --host gce" → Discord posts signed
   APPLICATION_COMMAND to /webhooks/discord
2. Router verifies Ed25519, returns {"type": 5} (defer) within 3s
3. BackgroundTask runs Dispatcher → ACL pass → OTP gate stashes →
   bot PATCHes @original with "Privileged. Reply with /otp ..."
4. /otp code:"..." → /otp builtin pops session, validates code,
   resumes service_restart handler → SSH on gce → SERVICE_EXECUTED
   audited with via_otp=true
5. Bot PATCHes @original with the SSH output
```

Each step shows up in `var/audit.jsonl` — same shape as Telegram, with
`platform=discord` distinguishing the source.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "Interactions Endpoint URL" save fails with red error | tunnel down / wrong path / `DISCORD_PUBLIC_KEY` mismatch / bot crashed | `just dev` running? check ngrok URL is `/webhooks/discord` not `/webhooks/telegram`; copy `DISCORD_PUBLIC_KEY` again from General Information; test `curl https://<tunnel>/webhooks/discord` returns `{"detail":"missing signature headers"}` (proves bot reachable) |
| `just register-discord` returns 401 | `DISCORD_BOT_TOKEN` is stale or wrong | dev portal → Bot → Reset Token, update `.env`, restart `just dev` |
| `/help` doesn't appear in Discord's autocomplete | commands registered globally (~1h delay) OR registered to a different guild | set `DISCORD_GUILD_ID` to your test server, re-run `just register-discord` |
| Bot replies "forbidden" to every command | the user's `discord:<id>` isn't in `config/app.yaml` users.aliases | add it (`/whoami` shows the norm_id you need) |
| `/service restart …` never returns a real reply, just silent | background task hit an exception | `tail -f` the `just dev` terminal output, check audit.jsonl, often an SSH connect / known_hosts issue |
| Discord shows "This interaction failed" 3 seconds after a command | the bot didn't respond within the 3s defer window | check `just dev` is actually running and the tunnel is alive; `curl` the endpoint to verify |

---

## Rotating leaked secrets

If `DISCORD_BOT_TOKEN` was exposed (committed to git, pasted into chat,
left in a screenshot, etc.):

1. Dev portal → **Bot** → **Reset Token** → copy the new value.
2. Update `.env`.
3. Restart `just dev`.

The old token is invalidated server-side at the moment of reset; no
log scrubbing needed. The bot will be offline for as long as it takes
you to update `.env` and restart.

`DISCORD_PUBLIC_KEY` and `DISCORD_APPLICATION_ID` are not secrets — no
rotation needed if they leak.

If `BOT_CMDER_MASTER_KEY` (TOTP encryption key, not Discord-specific)
leaked, see the rotation note in `README.md` — every TOTP enrollment
becomes invalid and users must re-enroll via `just enroll-totp`.
