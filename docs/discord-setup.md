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
| `DISCORD_GUILD_ID` | Discord client (not dev portal) — see step 2c | no | optional, recommended for dev |

> `DISCORD_GUILD_ID` is read **only** by `scripts/register_discord_commands.py`
> (the running bot never touches it). The `--guild=<id>` and `--global` CLI
> flags on that script can override the env value per call — see [§2c](#2c-discord_guild_id-optional-recommended-for-dev) and [§5](#5-register-the-slash-command-schema).

### 2a. `DISCORD_APPLICATION_ID` and `DISCORD_PUBLIC_KEY`

Both live on **General Information**. Copy them straight into `.env`:

```shell
echo "DISCORD_APPLICATION_ID=1500111111111111111" >> .env
echo "DISCORD_PUBLIC_KEY=<64-char-hex-from-General-Information>" >> .env
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
   echo "DISCORD_BOT_TOKEN=<paste-the-bot-token-shown-once>" >> .env
   ```

> ⚠️ **The token is a password.** Treat it accordingly:
> - Never commit `.env` (already gitignored).
> - Never paste the token into a chat, issue, or screenshot. (If you
>   do — see "Rotating leaked secrets" at the bottom.)
> - Resetting the token instantly invalidates the previous one. Any
>   running bot using the old token will go offline; you'll need to
>   restart it after updating `.env`.

### 2c. `DISCORD_GUILD_ID` (optional, recommended for dev)

Slash command registration can target one server — updates propagate
**instantly** instead of taking ~1 hour globally. Set this env var
once and `just register-discord` keeps using it; the CLI flags on
the register script let you override per call without touching `.env`.

To get the value:

1. In the Discord client (not the dev portal): **Settings** (gear) →
   **Advanced** → enable **"Developer Mode"**.
2. Right-click the server icon in the left sidebar → **"Copy Server
   ID"**.
3. Paste into `.env`:

   ```shell
   echo "DISCORD_GUILD_ID=<your-server-id>" >> .env
   ```

> **Bot doesn't read this.** `DISCORD_GUILD_ID` lives in `.env` only
> for ergonomics — `scripts/register_discord_commands.py` is the
> single consumer. The running bot doesn't care which guild it's in;
> Discord's interaction payloads carry `guild_id` per request.

Precedence when running `just register-discord`:

| | Effect |
|---|---|
| `--guild=<id>` flag | use this guild (overrides env) |
| `--global` flag | force global push (overrides env) |
| `DISCORD_GUILD_ID` in `.env` | use that guild (default) |
| (none of the above) | global push |

| | Guild push | Global push |
|---|---|---|
| Visible in | only that server | every server the bot is in + DMs |
| Time to propagate | **instant** | ~1 hour |
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

2. Sanity-check reachability **before** pasting into Discord (saves
   you a confusing "save fails" round-trip):

   ```shell
   # Both responses prove the route is mounted and the signature
   # gate is active — Discord will be happy on Save.
   curl -s     https://<your-ngrok-domain>/webhooks/discord
   # → {"detail":"Method Not Allowed"}    (GET hits the route, POST-only)
   curl -sX POST https://<your-ngrok-domain>/webhooks/discord
   # → {"detail":"missing signature headers"}    (signature verify active)
   ```

3. Dev portal → **General Information** → "Interactions Endpoint URL"
   textbox.
4. Paste the URL → **Save Changes**.

Discord immediately probes the endpoint with a signed PING. If signing
verification fails, the page refuses to save with a red error. When it
saves successfully, you've proven both:

- the tunnel routes correctly,
- `DISCORD_PUBLIC_KEY` matches the application's actual key,
- the bot is running and answering PING.

---

## 5. Register the slash command schema

```shell
# Default — uses DISCORD_GUILD_ID from .env if set (instant propagation),
# else pushes globally (~1h propagation).
just register-discord

# Override env, push to a specific guild for this call only:
just register-discord --guild=<your-server-id>

# Override env, force global push (use for the prod release):
just register-discord --global
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

Pushing to guild <your-server-id> (instant propagation)...
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
| "Interactions Endpoint URL" save fails with red error | tunnel down / wrong path / `DISCORD_PUBLIC_KEY` mismatch / bot crashed | `just dev` running? check ngrok URL is `/webhooks/discord` not `/webhooks/telegram`; copy `DISCORD_PUBLIC_KEY` again from General Information; the two `curl` commands in [§4 step 2](#4-set-the-interactions-endpoint-url) should both succeed |
| `curl GET /webhooks/discord` returns `Method Not Allowed` | **not a problem** — the route is POST-only by design | this 405 actually proves the route is mounted; pair it with the POST `curl` to confirm signature verify is active |
| `just register-discord` returns 401 | `DISCORD_BOT_TOKEN` is stale or wrong | dev portal → Bot → Reset Token, update `.env`, restart `just dev` |
| `just register-discord` says "Pushing globally" when you wanted guild-scoped | `DISCORD_GUILD_ID` not in `.env` AND no `--guild` flag | set the env var or pass `--guild=<id>` — see [§2c](#2c-discord_guild_id-optional-recommended-for-dev) for precedence |
| `/help` doesn't appear in Discord's autocomplete | commands registered globally (~1h delay) OR registered to a different guild | set `DISCORD_GUILD_ID` to your test server in `.env` (or pass `--guild=<id>`) and re-run `just register-discord` |
| Discord shows "該申請未受回應" / "This interaction failed" 3 seconds after a command | the bot didn't respond within the 3s defer window | `just dev` actually running? tunnel alive? re-run the two `curl` checks in [§4 step 2](#4-set-the-interactions-endpoint-url) |
| Bot replies "forbidden" to **every** command (including `/help` `/whoami`) | your `discord:<id>` isn't in `config/app.yaml` users.aliases | see [Gotcha 1](#common-acl--totp-gotchas) below |
| Bot replies "forbidden" to **PRIVILEGED** commands only (SAFE ones work) | `acl.commands` doesn't grant the role for the synthetic command name | see [Gotcha 2](#common-acl--totp-gotchas) below |
| `/otp` from Discord rejected even though Telegram OTP works | TOTP is enrolled per-norm_id, not per-user | see [Gotcha 3](#common-acl--totp-gotchas) below |
| `/kubectl args:"version --client"` → "subcommand 'version' not in allowlist" | second-layer kubectl whitelist refused | see [Gotcha 4](#common-acl--totp-gotchas) below |
| `/service restart …` never returns a real reply, just silent | background task hit an exception | `tail -f` the `just dev` terminal output, check audit.jsonl, often an SSH connect / known_hosts issue |

---

## Common ACL / TOTP gotchas

These are the four bumps everyone hits the first time they wire up
Discord (or any second platform). All four are config-only — no code
change needed.

### Gotcha 1: `discord:<id>` not in users.aliases → "forbidden" to everything

**Symptom**: even `/help` (SAFE) replies `forbidden`.

**Root cause**: ACL flow is `parse → resolve user → check`. If the
caller's `norm_id` doesn't map to any `users[]` entry, no role is
attached, so `default_allow_safe` doesn't match either.

**Diagnosis**:

```shell
tail -n 5 var/audit.jsonl | jq -c 'select(.event=="AUTH_DENIED")'
# Look at the "user" field — that's the discord:<id> you need to add.
```

**Fix**: paste it into your existing `users[]` entry's aliases
(same row as your Telegram alias — keep one canonical user with
multiple platform handles):

```yaml
users:
  - id: zonda
    aliases:
      - "telegram:1234567890"
      - "discord:1111111111111111111"   # ← add
    role: sre
```

`just dev` autoreloads on save (when `RELOAD=1`); no restart needed.

### Gotcha 2: PRIVILEGED command not in `acl.commands` → "forbidden" without OTP prompt

**Symptom**: SAFE commands (`/help`, `/whoami`, `/service info`,
`/service sysinfo`) work, but `/service args:"restart hello --host gce"`
replies `forbidden` and **never offers an OTP prompt**.

**Root cause**: PRIVILEGED commands default-deny. They only become
callable when explicitly granted in `acl.commands`. SAFE commands fall
back to `default_allow_safe`, which is why bare `/help` works while
`/service_restart` doesn't.

**Diagnosis**:

```shell
tail -n 5 var/audit.jsonl | jq -c 'select(.event=="AUTH_DENIED")'
# {"event":"AUTH_DENIED","command":"service_restart","user":"discord:..."}
# command field is the synthetic name dispatcher checks against acl.commands.
```

**Fix**: add the synthetic name (router_subcommand, with underscore)
to `config/app.yaml`:

```yaml
acl:
  default_allow_safe: ["role:sre", "role:viewer"]
  commands:
    kubectl:         ["role:sre"]
    runbook_run:     ["role:sre"]
    ssh:             ["role:sre"]
    service_restart: ["role:sre"]   # ← add
    service_logs:    ["role:sre"]   # ← add (next thing you'll hit)
```

> **Note**: this is identical between Telegram and Discord. If you
> ever see "Telegram works, Discord doesn't" for the same command,
> 99% of the time the Telegram call you remember was actually a SAFE
> action (`service_status` / `service_sysinfo`), not the privileged
> one. Both adapters share the same dispatcher and ACL.

### Gotcha 3: TOTP secret is per-platform — enroll twice

**Symptom**: Telegram `/otp` works, but Discord `/otp` says the code
is invalid (or there's no enrollment).

**Root cause**: `TOTPVerifier.verify(ctx.user.norm_id, code)` looks
up the secret keyed by `norm_id` (`telegram:111`, `discord:945...`),
not by canonical user id. Aliasing in `users[]` is for ACL only.

**Diagnosis**:

```shell
just list-totp
# Should list every norm_id that has a stored secret. If your
# discord:<id> isn't there, /otp can't possibly succeed.
```

**Fix**: enroll a separate TOTP secret per platform, scan the QR /
URI as a **new entry** in your authenticator app:

```shell
just enroll-totp discord:1111111111111111111
# Prints an otpauth:// URI — paste into Authy / 1Password / etc.
```

You'll end up with two authenticator entries
(`bot-cmder:telegram:1234567890` + `bot-cmder:discord:1111111111111111111`)
showing **different** 6-digit codes at any given moment. Use the one
that matches the platform you're invoking `/otp` from.

> Future hardening (Phase 6 candidate): resolve `norm_id → canonical
> user id` at lookup time so one enrollment covers all aliases.
> Requires a `secret_store.py` schema migration.

### Gotcha 4: `kubectl` has its own subcommand allowlist on top of ACL

**Symptom**: OTP succeeds, kubectl runs, but reply is
`subcommand 'version' not in allowlist: get, describe, logs, rollout, scale, top`.

**Root cause**: defense in depth — even after ACL grants `kubectl`
and OTP passes, the handler checks the first positional arg against
`config/app.yaml` `kubectl.allowed_subcommands`. The default is
read-only diagnostics (no `apply` / `delete` / `edit` / `version`).

**Fix options**:

- **Use an allowed subcommand** for smoke testing — `/kubectl args:"get nodes"`
  exercises the whole path including a real API call, much more
  meaningful than `version --client`.
- **Add to allowlist** if you genuinely need it:

  ```yaml
  kubectl:
    kubeconfig: /home/bot/.kube/config
    allowed_subcommands: [get, describe, logs, rollout, scale, top, version]
  ```

  Be conservative — adding `apply` or `delete` here turns the bot into
  a cluster-modifying tool, which is fine if intentional but a big
  blast-radius increase.

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
