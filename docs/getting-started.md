# Getting started — your first 30 minutes with bot-cmder

A linear walkthrough from zero to "I just ran a privileged command via TOTP from my phone." Six 5-minute steps, copy-paste friendly. After this you'll know the moving parts well enough to navigate the rest of the docs.

## What you'll have at the end

- `bot-cmder` running locally on your laptop
- A Telegram bot connected via polling mode (no public URL needed)
- A TOTP-enrolled SRE identity
- One SAFE command (`/health`) and one PRIVILEGED command (`/kubectl version` or `/ssh`) working end-to-end with the OTP gate

If you'd rather start with Discord or Slack: skim through this with Telegram, then jump to [`discord-setup.md`](discord-setup.md) or [`slack-setup.md`](slack-setup.md) — the platform-setup-specific dance is where they differ; everything else is identical.

## Prerequisites

- Python 3.10+
- A Telegram account (phone number)
- ~30 minutes uninterrupted

You don't need: a public domain, Docker, k8s, a server. Everything happens on your laptop.

---

## 5 min · Step 1 — Install + scaffold

```bash
pip install bot-cmder
```

(Or `uv tool install bot-cmder`, or use the [Docker image](docker.md) — see [`README.md`](../README.md#install) for the full menu of install paths.)

Run the scaffold:

```bash
bot-cmder init
```

Output:

```
Created /home/<you>/.config/bot-cmder/app.yaml (edit users + ACL before serve)
Created /home/<you>/.config/bot-cmder/.env (contains BOT_CMDER_MASTER_KEY — back this up)
Created /home/<you>/.local/state/bot-cmder/

Next steps:
  bot-cmder enroll-totp --user telegram:<your-id>
  bot-cmder serve
```

What was created:

- `~/.config/bot-cmder/app.yaml` — operator config (users, ACL, services, healthcheck targets). Verbatim copy of [`bot_cmder/data/app.yaml.example`](../bot_cmder/data/app.yaml.example), which is the **complete reference** for every settable field.
- `~/.config/bot-cmder/.env` — secrets, including a freshly-generated `BOT_CMDER_MASTER_KEY`. **Chmod 600** automatically — don't commit this file or share its contents.
- `~/.local/state/bot-cmder/` — empty state directory. `audit.jsonl` and `totp.sqlite` will materialize here on first use.

Verify your install:

```bash
bot-cmder --version       # → bot-cmder 0.X.Y
bot-cmder --help          # → 8 subcommands listed
```

> **Tip — re-running `init` is safe by default.** It refuses to overwrite without `--force`. With `--force`, the existing `BOT_CMDER_MASTER_KEY` is **preserved** so your TOTP enrollments survive. Pass `--rotate-key` only when you intentionally want to invalidate every enrollment.

---

## 5 min · Step 2 — Make a Telegram bot

Open Telegram, find [@BotFather](https://t.me/BotFather), send `/newbot`. Pick a display name + a username (must end in `bot`). BotFather replies with a token like `123456789:AAH-xxxxxxxxx`.

Add it to your `.env`:

```bash
echo 'TELEGRAM_TOKEN=123456789:AAH-xxxxxxxxx' >> ~/.config/bot-cmder/.env
echo 'TELEGRAM_MODE=polling' >> ~/.config/bot-cmder/.env
```

Why `TELEGRAM_MODE=polling`: in polling mode the bot dials OUT to Telegram's API every 25 seconds asking "any new messages?" — no public URL, no inbound port, no firewall config. This is the home-lab / laptop-friendly mode. Webhook mode (the default) needs a public HTTPS URL Telegram can POST to — overkill for your first run.

Full polling-mode walkthrough including reconnection behavior + drop-pending semantics: [`docs/telegram-polling.md`](telegram-polling.md).

---

## 5 min · Step 3 — First run + `/whoami`

Start the bot:

```bash
bot-cmder serve
```

You should see (color-coded in your terminal):

```
INFO  bot_cmder: TOTP enabled: secret_store=/home/<you>/.local/state/bot-cmder/totp.sqlite, session_ttl=120s
INFO  bot_cmder: ssh pool configured: 0 host(s) — (none)
INFO  bot_cmder: commands registered: help, whoami, health, otp
INFO  bot_cmder: telegram adapter mounted (mode=polling)
INFO     Started server process [...]
INFO     Application startup complete.
INFO     Uvicorn running on http://127.0.0.1:47823
```

Don't worry about the `127.0.0.1:47823` line — polling mode means the bot is dialing OUT to Telegram, so the local HTTP port is just for `/healthz`.

Open Telegram, find your new bot (search for the username BotFather gave you), and send:

```
/whoami
```

You should get a reply like:

```
norm_id: telegram:1234567890
role: viewer
```

Copy that `telegram:1234567890` — it's your SRE identity inside `bot-cmder`. Now grant yourself SRE role + access to SAFE commands. Open `~/.config/bot-cmder/app.yaml` in your editor and find the top:

```yaml
users:
  - id: zonda
    aliases: ["telegram:1234567890"]   # ← replace with YOUR norm_id from /whoami
    role: sre
```

Save. Restart the bot (Ctrl-C the running one, run `bot-cmder serve` again).

In Telegram, try a SAFE command:

```
/help
```

Should list every command available to you. ACL working ✓.

> **Tip — `bot-cmder serve --reload`** auto-restarts on yaml save. Useful while iterating on `app.yaml`. Don't use `--reload` in production — it's a dev-mode convenience.

---

## 5 min · Step 4 — Enroll TOTP

Privileged commands (anything that mutates state — `/kubectl`, `/ssh`, `/service restart`, etc.) require a 6-digit TOTP code from your phone. Enroll yourself:

```bash
bot-cmder enroll-totp --user telegram:1234567890
```

Output:

```
Enrolled: telegram:1234567890

Manual entry secret (base32):
  JBSWY3DPEHPK3PXP

Provisioning URI (use any QR generator, or paste into 1Password / Authy):
  otpauth://totp/bot-cmder:telegram:1234567890?secret=JBSWY3DPEHPK3PXP&issuer=bot-cmder

Render the QR in your terminal with one of:
  echo 'otpauth://...' | qrencode -t ANSI -o -
  echo 'otpauth://...' | python -m qrcode

Note: any previous TOTP enrollment for this user has been replaced.
```

Open your authenticator app (1Password / Authy / Google Authenticator / built-in iOS/Android), and either:

- **Scan the QR**: paste the URI into the QR-render command, then point your authenticator at the terminal
- **Manual entry**: type the base32 secret into the authenticator's "add account manually" flow

You should now see a 6-digit code rotating every 30s.

For the full TOTP design (replay protection, cross-chat enforcement, emergency-bypass mode for incidents): [`docs/otp.md`](otp.md).

---

## 5 min · Step 5 — First PRIVILEGED command

Restart the bot if it's not running. In Telegram:

```
/health
```

→ instant reply (this is SAFE, no OTP needed). Probably says "no targets configured" since we haven't added any healthcheck URLs yet.

Now try a PRIVILEGED one. The simplest is `/kubectl version` if you have `kubectl` installed:

```
/kubectl version --client
```

Bot replies:

```
Privileged command. Reply with: /otp <6-digit-code> within 120s
```

Glance at your authenticator app, type the current 6-digit code:

```
/otp 421783
```

Bot now actually runs `kubectl version --client` and replies with the output.

If you don't have `kubectl`, the same gate fires for any other PRIVILEGED command — `/ssh` (needs an SSH host configured first; see [`README.md`](../README.md) for that), or `/runbook run` (needs runbooks configured), or `/service restart` (needs services configured).

You've now exercised the full path: **chat → ACL → OTP gate → handler → audit log**. Tail the audit log to see what got recorded:

```bash
tail ~/.local/state/bot-cmder/audit.jsonl | jq
```

Each line is one JSON object with `ts`, `event`, `user`, `command`, `args` (redacted for sensitive args), `via_otp`, etc. The audit log [rotates daily at UTC midnight](audit-rotation.md) by default, gzipped, with a one-week retention window — no extra config needed.

---

## 5 min · Step 6 — Where next

You've covered the spine. Pick the next thread by what you want to do:

### Add more platforms
- **Discord** — [`docs/discord-setup.md`](discord-setup.md): Discord app + slash commands. Or [`docs/discord-gateway.md`](discord-gateway.md) for no-domain mode (with the slash-command UX trade-off).
- **Slack** — [`docs/slack-setup.md`](slack-setup.md): app manifest + signing secret. Or [`docs/slack-socket-mode.md`](slack-socket-mode.md) for no-domain mode.

### Connect to real infrastructure
- **SSH-reachable services** — uncomment the `hosts:` and `services:` blocks in `~/.config/bot-cmder/app.yaml`. The full schema (host address / user / `key_path` / `known_hosts` / `allowed_commands` regex; service `actions:` mapping action names → shell command lines) is shown in [`bot_cmder/data/app.yaml.example`](../bot_cmder/data/app.yaml.example). Once configured: `/service status api` fans out across all hosts; `/service restart api --host server-a` runs the restart action on one host (PRIVILEGED, OTP-gated).
- **Runbooks** — drop executable scripts (`*.sh`, `*.py`) into the `runbooks/` directory. `/runbook list` shows them; `/runbook run <name>` executes (PRIVILEGED).

### Operational hardening
- **TOTP details + emergency mode** — [`docs/otp.md`](otp.md). The `/otp emergency 15` flow opens a 15-min window where PRIVILEGED commands skip the gate (for incident-response shortcut typing).
- **Audit log forensics** — [`docs/audit-rotation.md`](audit-rotation.md). How rotation works, retention tuning, `zgrep`/`jq` patterns.
- **Containerize** — [`docs/docker.md`](docker.md). `docker pull ghcr.io/zondatw/bot-cmder:latest`, mount config + state, k8s + Compose examples.

### Reference material
- **Full config schema** — [`bot_cmder/data/app.yaml.example`](../bot_cmder/data/app.yaml.example). Every field, every default, every section tagged with the originating phase.
- **Per-release notes** — [`CHANGELOG.md`](../CHANGELOG.md).
- **Maintainer release procedure** — [`docs/release.md`](release.md).
- **Contributor workflow** — [`AGENTS.md`](../AGENTS.md). issue-first, atomic commits, PR-then-merge.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `bot-cmder: command not found` after `pip install` | Your shell hasn't picked up the new PATH entry | New terminal, or `hash -r` (bash/zsh). On macOS with `pyenv`, run `pyenv rehash`. |
| Telegram bot ignores you | Polling mode race after a previous webhook session | Check `bot-cmder serve` log for `409 Conflict`. The daemon auto-deletes the webhook at startup; if it didn't, manually `curl https://api.telegram.org/bot$TELEGRAM_TOKEN/deleteWebhook` once. |
| `/whoami` says role `viewer` even after editing `app.yaml` | Bot didn't reload | Restart with Ctrl-C → `bot-cmder serve`, OR start with `--reload` next time |
| `/otp 123456` says "no pending privileged command" | OTP timeout (default 120s) or sent in a different chat | Re-run the original command, then `/otp <code>` faster — same chat, same platform |
| `/otp` says "invalid code" | Clock drift on phone or laptop | TOTP needs ±30s clock sync. Check both devices. The bot accepts a ±1 step window already (default `valid_window=1`); larger drifts surface as "invalid". |
| `BOT_CMDER_MASTER_KEY is not set` on `enroll-totp` | The CLI is reading a different `.env` than expected | Run with `BOT_CMDER_CONFIG_DIR=~/.config/bot-cmder bot-cmder enroll-totp ...` to pin it explicitly. Or check [config-discovery search order](../README.md#config-file-locations). |
| Audit log not growing | `bot-cmder serve` writing to a different `state_dir` than you're tailing | `bot-cmder serve` log line `audit log: <path>` shows the resolved path. Or `BOT_CMDER_STATE_DIR=...` to pin. |
| `/kubectl version` fails with "kubectl not found" | The bot's PATH doesn't include `kubectl` | Either install kubectl on PATH, or set `kubectl.kubeconfig: /path/to/config` in `app.yaml` to pin a binary location. |

For anything else — open an issue at https://github.com/zondatw/bot-cmder/issues with the bot's log + the chat exchange that failed.
