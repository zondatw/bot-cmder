# bot-cmder

Multi-platform SRE ChatOps bot — drive maintenance operations from Telegram (Discord and Slack land in later phases) when you are away from a computer and prod has issues.

## What's in this release

**Phase 1** — multi-platform foundation
- FastAPI service exposing `POST /webhooks/telegram` and `GET /healthz`.
- Telegram adapter on `httpx` (no URL-encoding bugs from the legacy demo).
- `CommandRegistry` + decorator-style registration; `Dispatcher` with structured JSONL audit log.
- ACL via YAML config: per-command allow lists and `role:<name>` expansion.
- Builtins: `/help`, `/whoami`, `/health [target ...]`.

**Phase 2** — TOTP gate + privileged commands
- `BOT_CMDER_MASTER_KEY` + Fernet-encrypted SQLite secret store for TOTP secrets.
- Replay-protected `TOTPVerifier` over `pyotp`.
- OTP gate in `Dispatcher`: any `Risk.PRIVILEGED` command stashes a `PendingOTPSession` and asks the user for `/otp <code>` (configurable TTL, default 120s).
- `/otp` builtin enforces same-chat / same-platform OTP delivery, expiry, replay, and emits distinct audit events for each failure mode.
- Admin CLI: `python -m bot_cmder.cli {enroll,list,revoke}-totp` (or `just enroll-totp <user>`).
- `/kubectl <subcmd> [args...]` — whitelisted (default get/describe/logs/rollout/scale/top), KUBECONFIG honored, output truncated.
- `/runbook list` (SAFE) and `/runbook run <name> [args...]` (PRIVILEGED) — discovers executable scripts in `runbooks/`, rejects path traversal and shell metacharacters in args.

**Phase 3** — SSH connector + service actions ⭐ the core SRE use case
- `SshConnector` over `asyncssh`: one persistent connection per host with TTL-based reuse, strict `known_hosts` checking, key auth only.
- `SshConnectorPool` indexed by host name; lifecycle tied to FastAPI lifespan.
- YAML config for `hosts:` (address / user / key_path / allowed_commands) and `services:` (hosts list + action templates like `restart: "sudo systemctl restart api.service"`).
- `/service` is a **router** — adding `actions:` keys in yaml auto-registers `/service <action>` subcommands. No code change to add a new action; just yaml + restart.
  - Read-shaped action names (`status`, `logs`, `sysinfo`, `df`, `top`, `ps`, `tail`, ...) classify as SAFE, fan out across every host in parallel.
  - Everything else (`restart`, `deploy`, `drain`, ...) classifies as PRIVILEGED, TOTP-gated, refuses to run without explicit `--host X`.
  - `/service list` and `/service info <name>` are hardcoded metadata subcommands.
- `/runbook` is also a router with `list` (SAFE) and `run <name> [args...]` (PRIVILEGED).
- `/ssh <host> <cmd...>` (PRIVILEGED) — escape hatch for ad-hoc commands; refused unless the joined command fully matches at least one regex in the host's `allowed_commands`.

**Phase 4** — Discord adapter
- `POST /webhooks/discord` with PyNaCl Ed25519 signature verification (Discord refuses to onboard endpoints that don't sign-check).
- PING (type 1) → answered inline; APPLICATION_COMMAND (type 2) → defer (`type: 5`) immediately, run dispatch in a `BackgroundTask`, then PATCH `@original` via the per-interaction webhook URL. Honors Discord's 3-second initial-response cap even for slow handlers (SSH, OTP gate).
- `DiscordClient` handles both deferred follow-ups (no auth — interaction token IS the capability) and slash command registration (bot token).
- `scripts/register_discord_commands.py` (`just register-discord`) PUT-replaces the slash command schema. Builds the manifest from the live registry so /service action subcommands derived from yaml show up in Discord's autocomplete.
- Slash commands are flat: each Command and Router gets one top-level `/cmd` with a single STRING `args` option (or `code` for `/otp`). The adapter recombines `/<cmd> <args>` back into the same text-shaped IncomingMessage Telegram produces — same dispatch flow on both platforms.

The Slack adapter arrives in Phase 5.

## Setup

Requires Python 3.10+ and [`uv`](https://github.com/astral-sh/uv).

```shell
uv sync
pre-commit install --install-hooks
```

Create a `.env` (see `.env.example`):

```
TELEGRAM_TOKEN=xxxxxxxxxxxxx
TELEGRAM_WEBHOOK_SECRET=any-long-random-string
APP_CONFIG_PATH=./config/app.yaml

# Phase 2 — required for /kubectl, /runbook run and any Risk.PRIVILEGED command
BOT_CMDER_MASTER_KEY=<run `just gen-master-key` to generate>
```

Create `config/app.yaml` (see `config/app.yaml.example`):

```yaml
users:
  - {id: zonda, aliases: ["telegram:111111111"], role: sre}
acl:
  default_allow_safe: ["role:sre"]
healthcheck:
  targets:
    - {name: api, url: https://api.example.com/healthz}
audit:
  path: ./var/audit.jsonl
```

## Run

```shell
uv run python server.py
```

Local dev binds `127.0.0.1:47823` by default (intentionally uncommon to avoid clashing with other services on 8000 / 8080). Override with `BIND_HOST`, `BIND_PORT`, or `RELOAD=1` for autoreload.

## Justfile shortcuts

```shell
just                                  # list tasks
just dev                              # uvicorn with reload
just tunnel-ngrok                     # ngrok with reserved static domain (recommended)
just tunnel                           # cloudflared quick tunnel (random subdomain each run)
just hook-status                      # show Telegram webhook url / pending / last error
just hook-watch                       # watch hook-status every 2s
just test                             # pytest
just lint                             # ruff + black --check
just show-env-settings                # echo env vars
just set-telegram-bot-webhook         # POST setWebhook to Telegram (manual; tunnel does this for you)
just gen-master-key                   # generate a fresh BOT_CMDER_MASTER_KEY for .env
just register-discord                 # push slash command schema to Discord (Phase 4)
just enroll-totp <user>               # enroll a user for TOTP (e.g. telegram:111)
just list-totp                        # list TOTP-enrolled users
just revoke-totp <user>               # drop a user's TOTP enrollment
```

Typical local dev loop: one terminal `just dev`, another `just tunnel-ngrok`.
Both tunnel recipes auto-update `.env` and re-register the webhook.

### Tunnel: ngrok vs cloudflared

| | `just tunnel-ngrok` | `just tunnel` |
|---|---|---|
| URL stability | static, never changes | fresh random `*.trycloudflare.com` per run |
| DNS reliability | permanent record, always warm | 30–90s propagation lag, hits negative-cache failures |
| Setup cost | one-time signup + reserve domain | none |
| Recommended for | daily dev | quick experiments only |

**ngrok one-time setup** (recommended path):

```shell
brew install ngrok
ngrok config add-authtoken <token from https://dashboard.ngrok.com>
# Reserve a free domain at https://dashboard.ngrok.com/domains, then:
echo 'NGROK_DOMAIN=your-name.ngrok-free.app' >> .env
just tunnel-ngrok
```

## TOTP enrollment (Phase 2)

Privileged commands (`/kubectl`, `/runbook run`) require a TOTP code
delivered out-of-band, stored per-user encrypted at rest with
`BOT_CMDER_MASTER_KEY`.

```shell
# 1. Generate a master key once and put it in .env.
echo "BOT_CMDER_MASTER_KEY=$(just gen-master-key)" >> .env

# 2. Enroll yourself; copy the otpauth:// URI into 1Password /
#    Authy / Google Authenticator (or pipe through `qrencode`).
just enroll-totp telegram:111111111

# 3. Restart the bot so it picks up the new key.
just dev
```

Then in the bot DM:

```
/kubectl get pods
  → "Privileged command. Reply with: /otp <6-digit-code> within 120s"
/otp 654321
  → kubectl output
```

Audit log records every step: `OTP_REQUESTED`, `OTP_INVALID`,
`OTP_CROSS_CHAT`, `OTP_EXPIRED`, `EXECUTED ... via_otp=true`.

## SSH host setup (Phase 3)

`/service *` and `/ssh` need `config.hosts` populated and the bot
process able to SSH into each host non-interactively.

```shell
# 1. On the bot host: generate a dedicated SSH key per remote host
#    (one key per host limits blast radius if any single one leaks).
ssh-keygen -t ed25519 -N "" -f /etc/bot-cmder/keys/server-a.ed25519 \
           -C "bot-cmder@server-a"

# 2. On the remote: install the public key for a low-priv account
#    that has the specific sudo rules you need.
#    (sudoers: `bot ALL=(root) NOPASSWD: /bin/systemctl restart api.service`)
ssh-copy-id -i /etc/bot-cmder/keys/server-a.ed25519.pub deploy@10.0.1.5

# 3. Pin the host key (strict checking is on by default).
ssh-keyscan -H 10.0.1.5 >> ~/.ssh/known_hosts
```

Then add the host to `config/app.yaml`:

```yaml
hosts:
  server-a:
    address: 10.0.1.5
    user: deploy
    key_path: /etc/bot-cmder/keys/server-a.ed25519
    allowed_commands:
      - "^sudo systemctl (status|restart) api\\.service$"

services:
  api:
    hosts: [server-a]
    actions:
      status:  "sudo systemctl status api.service"      # SAFE  fan-out
      logs:    "sudo journalctl -u api.service -n 100"  # SAFE  fan-out
      sysinfo: "uname -a"                               # SAFE  fan-out
      restart: "sudo systemctl restart api.service"     # PRIV  --host required

acl:
  commands:
    # PRIVILEGED actions auto-classify if you don't list them, but
    # adding a role rule here narrows it further.
    service_restart: ["role:sre"]
    ssh:             ["role:sre"]
```

Then in the bot DM:

```
/service                              → router help (lists subcommands)
/service list                         → lists configured services + hosts
/service info api                     → shows api's hosts + each action's command
/service status api                   → fan-out across api's hosts
/service sysinfo api                  → fan-out (action name → SAFE)
/service restart api --host server-a  → asks for /otp, then SSH-restarts
/ssh server-a sudo systemctl status api.service   → asks for /otp, allowlist-checked
```

### Adding a new action

Want `/service diskfree api`? Edit yaml, restart, done:

```yaml
services:
  api:
    actions:
      diskfree: "df -h | head"   # 'df' is in _SAFE_ACTION_NAMES → auto-SAFE + fan-out
```

Want `/service drain api --host X`? Same thing — `drain` is not a
read-shaped name, so it auto-classifies as PRIVILEGED:

```yaml
actions:
  drain: "kubectl drain $(hostname) --ignore-daemonsets"
```

Then optionally narrow ACL:

```yaml
acl:
  commands:
    service_drain: ["role:sre"]
```

## Discord setup (Phase 4)

Quick path:

```shell
# 1. dev portal → New Application; grab the three values:
#    https://discord.com/developers/applications
echo "DISCORD_APPLICATION_ID=..." >> .env  # General Info → Application ID
echo "DISCORD_PUBLIC_KEY=..."     >> .env  # General Info → Public Key
echo "DISCORD_BOT_TOKEN=..."      >> .env  # Bot → Reset Token (shown ONCE)

# 2. OAuth2 → URL Generator → check `bot` + `applications.commands`,
#    open the generated URL, invite the bot to your test server.

# 3. dev portal → General Info → Interactions Endpoint URL:
#    https://<your-ngrok>.ngrok-free.app/webhooks/discord
#    (just dev must be running first)

# 4. Push the slash command schema. For dev, scope to one guild so
#    updates propagate instantly (right-click server → Copy Server ID;
#    Developer Mode must be on in client Settings → Advanced):
just register-discord --guild=945123456789012345
# For prod (visible in every guild + DMs, ~1h propagation):
just register-discord

# 5. In Discord:
/help
/service args:"restart api --host gce"
/otp code:"123456"
```

For the **full walkthrough** — where each value comes from in the dev
portal, OAuth2 permission discussion, troubleshooting table, and
secret rotation steps — see [`docs/discord-setup.md`](docs/discord-setup.md).

## Tests

```shell
uv run pytest --cov
```

## Architecture

```
adapters/   how humans talk to the bot         (Telegram / Discord / Slack)
connectors/ how the bot talks to infra         (Local / SSH / ...)
core/       events, registry, dispatcher       (platform-neutral)
auth/       ACL whitelist + (Phase 2) TOTP
audit/      append-only JSONL audit log
commands/   builtin + user-supplied commands
config/     pydantic-settings (env) + YAML
```

The full plan and roadmap live in `docs/PLAN.md`.
