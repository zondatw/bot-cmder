# Slack Setup

End-to-end walkthrough for wiring `bot-cmder` into Slack. Every value
you need can be found inside Slack's app management UI; this doc
points at exactly where.

> **Prerequisite**: a Slack workspace where you have permission to
> create a custom app (most workspaces let any member do this), the
> bot already runs locally with `just dev`, and a public HTTPS URL
> reachable by Slack (`just tunnel-ngrok` is fine for dev).

---

## 1. Create the Slack app

1. Go to <https://api.slack.com/apps>.
2. Click **"Create New App"** → **"From scratch"**.
3. Pick a name (e.g. `sre_bot`) and the target workspace.
4. After it lands on the app page, the left sidebar has the four
   sections we'll touch:
   - **Basic Information** (signing secret + bot token)
   - **OAuth & Permissions** (scopes + bot install)
   - **Slash Commands** (one entry per `/cmd` we expose)
   - **Event Subscriptions** (only needed to enable URL verification, optional)

---

## 2. Pull the two environment variables

| `.env` key | Where in app config | Secret? | Required? |
|---|---|---|---|
| `SLACK_SIGNING_SECRET` | **Basic Information** → "App Credentials" → "Signing Secret" | **yes** | ✅ |
| `SLACK_BOT_TOKEN` | **OAuth & Permissions** → "Bot User OAuth Token" (`xoxb-...`) | **yes** | optional (Phase 5 doesn't use it; Phase 6 will) |

### 2a. `SLACK_SIGNING_SECRET`

1. Left sidebar → **Basic Information**.
2. Scroll to "App Credentials".
3. Next to "Signing Secret", click **"Show"** then copy.
4. Paste into `.env`:

   ```shell
   echo "SLACK_SIGNING_SECRET=abc123def456..." >> .env
   ```

> ⚠️ **The signing secret is a password.** It's the only thing
> stopping anyone on the internet from forging slash commands to your
> endpoint. Treat it like any other secret — never commit `.env`
> (already gitignored), never paste into chat. If it leaks, regenerate
> from the same UI; old signatures stop validating immediately.

### 2b. `SLACK_BOT_TOKEN` (optional for Phase 5)

You'll need this **after** the OAuth install in [§3](#3-add-scopes-and-install-the-bot-into-the-workspace) below.

---

## 3. Add scopes and install the bot into the workspace

1. Left sidebar → **OAuth & Permissions**.
2. Scroll to "Scopes" → "Bot Token Scopes" → **Add an OAuth Scope**.
3. Add the minimal scope set for slash commands:
   - `commands` — required for any `/cmd` to fire
   - `chat:write` — reserved for Phase 6 (Block Kit posting); harmless
     to grant now so you don't have to re-install later
4. Scroll to the top of the page → **"Install to Workspace"** →
   **Allow** in the consent screen.
5. After install, the page now shows **"Bot User OAuth Token"**
   starting with `xoxb-`. Copy and paste:

   ```shell
   echo "SLACK_BOT_TOKEN=xoxb-1234567890-abcdefghij" >> .env
   ```

---

## 4. Set the Request URL on **every** slash command

Slack registers each `/cmd` separately (unlike Discord's bulk PUT) —
expect to repeat this once per command (currently 8). The Request
URL is the same for all of them: `https://<your-tunnel>/webhooks/slack`.

1. Make sure `just dev` is running and your tunnel is up. If you
   already set up Telegram with `just tunnel-ngrok`, the same tunnel
   works here — just append `/webhooks/slack` to the public URL.

   ```
   https://<your-ngrok-domain>.ngrok-free.dev/webhooks/slack
   ```

2. Sanity-check reachability **before** going to the Slack app config:

   ```shell
   curl -s     https://<your-ngrok-domain>/webhooks/slack
   # → {"detail":"Method Not Allowed"}    (route mounted, POST-only)
   curl -sX POST https://<your-ngrok-domain>/webhooks/slack
   # → {"detail":"bad signature"}         (signature gate active)
   ```

3. **Generate the manifest** from the live registry instead of
   hand-creating 8 separate slash commands in the UI:

   ```shell
   # Tells the script where Slack should POST. Bare hostname OK —
   # the script appends /webhooks/slack. Falls back to NGROK_DOMAIN
   # if you've already set that for `just tunnel-ngrok`.
   echo "SLACK_REQUEST_URL=<your-ngrok-domain>" >> .env

   just register-slack > /tmp/slack-manifest.yaml
   ```

   The script reads the registry exactly the same way `main.py` does,
   so the manifest reflects what the running bot actually accepts —
   add a builtin or change a description, re-run, you get the new
   manifest. **Single source of truth: the registry.** No hand-
   maintained manifest file to drift.

4. Slack app page → **Features** → **App Manifest** tab → paste the
   contents of `/tmp/slack-manifest.yaml` over whatever's there → click
   **Save Changes** → Slack pops a "this will reinstall the app"
   confirmation → **Yes**.

   > Slack lacks Discord's bot-token-authed PUT API for slash
   > commands; manifest updates require [config tokens](https://api.slack.com/authentication/config-tokens)
   > that rotate per use. Until that friction is worth it (Phase 6+),
   > the script just generates the manifest — paste-into-UI is one
   > extra click vs. Discord's `just register-discord`.

5. After save, the page now lists all 8 commands under **Slash Commands**:

   | Command | Description |
   |---|---|
   | `/help` | Show available commands |
   | `/whoami` | Show your normalized identity and role |
   | `/health` | HTTP healthcheck against configured targets |
   | `/service` | Router (status / sysinfo / restart / ...) |
   | `/ssh` | Run a remote command on a configured host |
   | `/kubectl` | Run a whitelisted kubectl subcommand |
   | `/runbook` | List and run pre-deployed shell scripts |
   | `/otp` | Submit OTP code for a pending privileged command |

   > Slack ignores any structured schema beyond name + URL — there's
   > no equivalent of Discord's typed STRING options. Every command
   > is just "send whatever the user typed as `text`", which the
   > adapter recombines into the canonical `/cmd args…` form before
   > dispatch.

---

## 5. (Optional) Set up Events API for URL verification

Phase 5 only handles slash commands, so this section is **skip-able**.
Enable it now only if you plan to do Phase 6 socket mode or future
@-mention handling.

1. Left sidebar → **Event Subscriptions** → toggle **On**.
2. **Request URL**: same `/webhooks/slack` URL as above. Slack will
   immediately POST a `url_verification` challenge; the bot echoes
   back the challenge value, Slack accepts the URL with a green tick.
3. (Don't subscribe to any events for Phase 5; an empty subscription
   list is fine.)

---

## 6. Add yourself to the ACL

Before any command works, your `slack:U<id>` norm_id must appear in
`config/app.yaml` `users.aliases`. Two ways to discover it:

### 6a. From the Slack client (fastest)

1. Click your own avatar in the bottom-left of the Slack client →
   **View profile**.
2. On the profile card that pops up, click **⋮** (three-dot menu) →
   **Copy member ID**.
3. You get something like `U0123ABCD` (no `slack:` prefix). Paste
   into `config/app.yaml`, prepended with `slack:`:

   ```yaml
   users:
     - id: zonda
       aliases:
         - "telegram:1234567890"
         - "discord:1111111111111111111"
         - "slack:U0123ABCD"          # ← add
       role: sre
   ```

### 6b. From the audit log (most reliable, shared with Discord/Telegram)

Doesn't require Slack UI access; works if a teammate runs the bot
on a host you don't sit at:

1. Type `/help` in Slack from the user account you want to ACL-add.
2. Bot replies `forbidden` (because you're not in `users.aliases` yet).
3. The denial is logged with the **exact** norm_id the dispatcher
   checked against:

   ```shell
   tail -n 5 var/audit.jsonl | jq -c 'select(.platform=="slack" and .event=="AUTH_DENIED")'
   # {"event":"AUTH_DENIED","user":"slack:U0123ABCD","chat":"C...",...}
   ```

4. Copy the `user` field verbatim into `users.aliases` as above.

> Methods 6a and 6b should produce the same value. If they don't,
> trust the audit log — that's what the bot actually sees. Diverging
> values usually mean a multi-workspace Enterprise Grid install, in
> which case the audit log carries the workspace-local ID the
> dispatcher uses.

`just dev` autoreloads on yaml save (with `RELOAD=1`); no restart needed.

### 6c. Enroll TOTP for the new norm_id

TOTP secrets are keyed by norm_id, not by canonical user — so the
secret you enrolled for Telegram or Discord doesn't carry over. One
extra entry in your authenticator app:

```shell
just enroll-totp slack:U0123ABCD       # prints otpauth:// URI / QR
just list-totp                          # confirms it landed
```

(Background: see Discord [Gotcha 3](discord-setup.md#common-acl--totp-gotchas)
for the design rationale and the Phase 7 hardening candidate that
would unify enrollments across platforms.)

---

## 7. Verify in chat

In any channel where the bot is invited (or in a DM):

```
/help                                            # listing of registered commands
/whoami                                          # confirms your slack:U<id>
/service info hello                              # service detail dump
/service sysinfo hello                           # SAFE action, fan-out
/service restart hello --host gce                # PRIVILEGED → /otp prompt
/otp 123456                                      # → SSH executes
```

Behind the scenes:

```
1. /service restart hello --host gce → Slack POSTs signed
   x-www-form-urlencoded body to /webhooks/slack
2. Router verifies HMAC-SHA256(`v0:<ts>:<body>`), returns 200 ack
   immediately
3. BackgroundTask runs Dispatcher → ACL pass → OTP gate stashes →
   bot POSTs response_url with "Privileged. Reply with /otp …"
4. /otp 123456 → /otp builtin pops session, validates code,
   resumes service_restart → SSH on gce → SERVICE_EXECUTED
   audited with via_otp=true
5. Bot POSTs response_url with the SSH output
```

Each step shows up in `var/audit.jsonl` — same shape as Telegram +
Discord, with `platform=slack` distinguishing the source.

---

## 8. Reply visibility — `ephemeral` vs `in_channel`

Slack slash commands can reply two ways:

- **ephemeral** — only the invoker sees it (private, default-safe)
- **in_channel** — the whole channel sees it (great for collaborative
  ops: teammates can spot each other's `status` / `health` checks
  without re-asking)

Configure in `config/app.yaml`:

```yaml
slack:
  # Global default. One of:
  #   by_risk    — SAFE → in_channel, PRIVILEGED → ephemeral (recommended)
  #   in_channel — everything is broadcast (good for solo dev / private channel)
  #   ephemeral  — everything is private (good for sensitive workspace)
  reply_visibility: by_risk

  # Per-command tweaks. Key = synthetic command name from audit log
  # (e.g. service_restart, not "service restart"). Value =
  # "in_channel" or "ephemeral".
  visibility_overrides:
    whoami: ephemeral             # don't broadcast user IDs
    service_logs: ephemeral       # logs may carry sensitive data
    help: in_channel              # team learns by lurking
```

| Mode | Effect |
|---|---|
| `by_risk` (default) | SAFE → in_channel, PRIVILEGED → ephemeral |
| `in_channel` | every reply visible to everyone |
| `ephemeral` | every reply private to invoker |
| per-command override | wins over the global mode for that command only |

---

## 9. (Optional) Socket Mode — no public URL needed

If you don't have a public HTTPS URL (home lab, NAT, restrictive
egress), Slack's **Socket Mode** is a drop-in alternative. The bot
opens a WebSocket OUT to Slack and slash commands arrive over that
connection — no `/webhooks/slack` endpoint, no tunnel.

Quick switch:

```shell
# 1. In Slack app config: enable Socket Mode toggle, generate
#    "App-Level Token" with connections:write scope (xapp-...)
# 2. .env:
echo "SLACK_MODE=socket"        >> .env
echo "SLACK_APP_TOKEN=xapp-..." >> .env
# 3. Restart `just dev` — log should show "mode=socket" + "hello"
```

Full walkthrough + protocol details + troubleshooting:
[`docs/slack-socket-mode.md`](slack-socket-mode.md).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Slack app page shows red "Your URL didn't respond with a value of the challenge parameter" | URL wrong / bot down / signing secret mismatch | check the two `curl` commands in [§4 step 2](#4-set-the-request-url-on-every-slash-command) succeed; copy `SLACK_SIGNING_SECRET` again from Basic Information |
| `curl GET /webhooks/slack` returns `Method Not Allowed` | **not a problem** — route is POST-only by design | this 405 actually proves the route is mounted; pair with the POST `curl` to confirm signature gate is active |
| `/help` typed in Slack returns `dispatch_failed` | bot not running OR endpoint URL not saved on that specific command | check `just dev` is up and the **Slash Commands** entry for `/help` has the right Request URL filled in |
| Slash command works for SAFE only; PRIVILEGED replies "forbidden" without OTP prompt | `acl.commands` doesn't grant the role for the synthetic command name | see Discord docs [Gotcha 2](discord-setup.md#common-acl--totp-gotchas) — same root cause; add e.g. `service_restart: ["role:sre"]` |
| `/otp` rejected on Slack even though Telegram OTP works | TOTP enrolled per-norm_id, not per-canonical-user | run `just enroll-totp slack:U0123ABCD` (norm_id from [§6](#6-add-yourself-to-the-acl)); see Discord [Gotcha 3](discord-setup.md#common-acl--totp-gotchas) |
| `/whoami` reveals user IDs to the whole channel | `reply_visibility: by_risk` makes SAFE commands public | add `whoami: ephemeral` to `slack.visibility_overrides` |
| Reply takes 3+ seconds, channel shows "dispatch_failed" briefly | normal — bot acks within 3s and posts real reply via `response_url` (up to 30 min later); if you see `dispatch_failed` PERSISTING, the bot crashed or tunnel dropped | check `just dev` stderr; the BackgroundTask exception, if any, is logged with the chat_id |

---

## Rotating leaked secrets

If `SLACK_SIGNING_SECRET` was exposed (committed to git, pasted into
chat, left in a screenshot, etc.):

1. Slack app → **Basic Information** → "App Credentials" →
   **"Regenerate"** next to Signing Secret.
2. Update `.env`.
3. Restart `just dev`.

The old secret stops validating signatures the moment Slack
regenerates; any in-flight slash command using the old key will fail
HMAC verification (operator sees `dispatch_failed` once and retries).

If `SLACK_BOT_TOKEN` was exposed: **OAuth & Permissions** →
**Revoke Token** → **Reinstall to Workspace** (you'll get a fresh
`xoxb-...`).

If `BOT_CMDER_MASTER_KEY` (TOTP encryption key, not Slack-specific)
leaked, see the rotation note in `README.md` — every TOTP enrollment
becomes invalid and users must re-enroll via `just enroll-totp`.
