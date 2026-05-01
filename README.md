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
- `/runbook-list` (SAFE) and `/runbook-run <name> [args...]` (PRIVILEGED) — discovers executable scripts in `runbooks/`, rejects path traversal and shell metacharacters in args.

**Phase 3** — SSH connector + service actions ⭐ the core SRE use case
- `SshConnector` over `asyncssh`: one persistent connection per host with TTL-based reuse, strict `known_hosts` checking, key auth only.
- `SshConnectorPool` indexed by host name; lifecycle tied to FastAPI lifespan.
- YAML config for `hosts:` (address / user / key_path / allowed_commands) and `services:` (hosts list + action templates like `restart: "sudo systemctl restart api.service"`).
- `/service-list` (SAFE), `/service-status <name>` (SAFE; fan-out across all hosts in parallel) — read paths.
- `/service-restart <name> --host X` and `/service-logs <name> --host X` (PRIVILEGED; TOTP-gated) — write paths require an explicit `--host`, no implicit fan-out.
- `/ssh <host> <cmd...>` (PRIVILEGED) — escape hatch for ad-hoc commands; refused unless the joined command fully matches at least one regex in the host's `allowed_commands`.

The Discord / Slack adapters arrive in subsequent phases.

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

# Phase 2 — required for /kubectl, /runbook-run and any Risk.PRIVILEGED command
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

Privileged commands (`/kubectl`, `/runbook-run`) require a TOTP code
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

`/service-*` and `/ssh` need `config.hosts` populated and the bot
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
      status:  "sudo systemctl status api.service"
      restart: "sudo systemctl restart api.service"
      logs:    "sudo journalctl -u api.service -n 100 --no-pager"

acl:
  commands:
    service-restart: ["role:sre"]
    service-logs:    ["role:sre"]
    ssh:             ["role:sre"]
```

Then in the bot DM:

```
/service-list                          → lists configured services + hosts
/service-status api                    → fans out across api's hosts in parallel
/service-restart api --host server-a   → asks for /otp, then SSH-restarts
/ssh server-a sudo systemctl status api.service   → asks for /otp, allowlist-checked
```

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
