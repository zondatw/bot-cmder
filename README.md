# bot-cmder

[![CI](https://github.com/zondatw/bot-cmder/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/zondatw/bot-cmder/actions/workflows/ci.yml)

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
- Admin CLI: `bot-cmder {enroll,list,revoke}-totp --user <id>` (or `just enroll-totp <user>`).
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
- `bot-cmder discord-register` (or `just register-discord`) PUT-replaces the slash command schema. Builds the manifest from the live registry so /service action subcommands derived from yaml show up in Discord's autocomplete.
- Slash commands are flat: each Command and Router gets one top-level `/cmd` with a single STRING `args` option (or `code` for `/otp`). The adapter recombines `/<cmd> <args>` back into the same text-shaped IncomingMessage Telegram produces — same dispatch flow on both platforms.

**Phase 5** — Slack adapter
- `POST /webhooks/slack` with HMAC-SHA256 signature verification (`v0:<ts>:<body>`, ±5-min replay window, constant-time compare).
- One endpoint handles both slash commands (form-encoded) and the Events API `url_verification` challenge (JSON) — distinguished by Content-Type, signature gate runs before either branch.
- Defer-then-respond pattern mirrors Discord: 200 OK ack within 3s, BackgroundTask dispatches, real reply POSTed to Slack's per-invocation `response_url` (one-shot, 30-min TTL, self-authorizing — no bot token needed).
- Reply visibility tunable per workspace via `config/app.yaml`:
  - `reply_visibility: by_risk` (default) — SAFE commands (`/help`, `/health`, `/service status`) broadcast to channel for collaborative ops; PRIVILEGED ones (`/service restart`, `/ssh`, `/kubectl`) stay ephemeral so SSH output isn't leaked.
  - `in_channel` / `ephemeral` — global override.
  - `visibility_overrides: {whoami: ephemeral, ...}` — per-command tweak (key = synthetic name from audit log).
- `/otp` propagates the resumed command's risk to the response so the visibility resolver treats `/otp 123456 → service_restart` reply as PRIVILEGED, not as `/otp`'s SAFE.
- Pure stdlib HMAC + httpx — no `slack-sdk` dependency.

**Phase 6a** — Telegram polling (no-domain mode)
- `TELEGRAM_MODE=polling` flips the adapter from inbound webhook to outbound `getUpdates` long-poll. No public URL, no tunnel, no firewall ports — bot only makes outgoing HTTPS calls. Ideal for home labs / NAT / restrictive corp egress.
- `TelegramDaemon` runs inside the FastAPI lifespan as an asyncio task, owns the offset bookkeeping, and shares the existing `Dispatcher` / OTP gate / audit logger — `/cmd` UX stays identical to webhook mode.
- Auto-deletes any registered webhook at startup so flipping mode Just Works (no manual `deleteWebhook` step). 409 conflicts are fatal — the daemon won't silently re-delete a webhook someone re-registered, forcing the operator to pick a side.
- Exponential backoff (1→30s) on transient errors; clean cancellation via `CancelledError` on lifespan shutdown.
- Tunable `TELEGRAM_POLLING_TIMEOUT_S` (default 25) and `TELEGRAM_POLLING_DROP_PENDING` (default false). Full walkthrough: [`docs/telegram-polling.md`](docs/telegram-polling.md).

**Phase 6b** — Slack Socket Mode (no-domain mode)
- `SLACK_MODE=socket` flips the adapter from inbound HTTP webhook to outbound WebSocket. Slash commands arrive over the socket Slack pushes back through; no public URL needed. Same dispatcher / OTP gate / audit log; same `/cmd` UX as Events API mode.
- `SlackSocketDaemon` opens a fresh WSS URL via `apps.connections.open`, ACKs each envelope within 3s, dispatches in a separate task to keep the read loop unblocked, and reconnects on Slack's graceful `disconnect` event (~30 min cycle). Cancellation via `CancelledError` on FastAPI lifespan shutdown.
- Mutex with Events API enforced **app-side** by Slack's UI (Socket Mode toggle greys out the Events URL), so the bot just needs `SLACK_MODE` to match the app config.
- New env: `SLACK_APP_TOKEN` (xapp-..., from "App-Level Tokens" → `connections:write` scope). 401/403 from `apps.connections.open` is fatal — bad token won't be retried, daemon exits with clear log.
- Hand-rolled WebSocket client against the `websockets` library — no `slack-sdk` dependency. Full walkthrough: [`docs/slack-socket-mode.md`](docs/slack-socket-mode.md).

**Phase 6c** — Discord Gateway (no-domain mode, with a UX trade-off)
- `DISCORD_MODE=gateway` flips the adapter from inbound HTTP Interactions to outbound WebSocket. Same dispatcher / OTP gate / audit log; **but Discord doesn't deliver slash commands over the Gateway** — UX shifts to `@bot cmd args` in guild channels or plain `cmd args` in DMs. The adapter normalizes both into the canonical `/cmd args` text shape so the dispatcher path stays byte-equivalent to Interactions mode.
- `DiscordGatewayDaemon` implements the full Gateway state machine: HELLO → IDENTIFY (or RESUME if session_id is preserved across reconnect) → READY → DISPATCH events with sequence tracking. HEARTBEAT every interval Discord asks for; 2 unacked = zombied, force-close + RESUME. INVALID_SESSION (resumable=False) wipes state for a clean re-IDENTIFY. Hand-rolled against `websockets` — no Discord SDK.
- Requires the **MESSAGE_CONTENT privileged intent** enabled in the dev portal (Bot → Privileged Gateway Intents). Without it the daemon connects fine but reads empty `content` strings — documented as the #1 gotcha.
- D3 dual-mode posture: pick ONE per deployment instance (env-validated at startup). Prod with public URL → `interactions`; home lab without → `gateway`. Both can coexist across instances. Full walkthrough: [`docs/discord-gateway.md`](docs/discord-gateway.md).

## Install

Requires Python 3.10+.

### From PyPI (recommended)

```shell
pip install bot-cmder
bot-cmder init                              # scaffold ~/.config/bot-cmder/{app.yaml,.env} + state dir
# edit ~/.config/bot-cmder/app.yaml — add your users + ACL
bot-cmder enroll-totp --user telegram:<your-id>
bot-cmder serve
```

`bot-cmder init` writes `~/.config/bot-cmder/app.yaml` (verbatim copy of [`bot_cmder/data/app.yaml.example`](bot_cmder/data/app.yaml.example) — full reference of every settable field), `~/.config/bot-cmder/.env` (with a freshly generated `BOT_CMDER_MASTER_KEY`, chmod 600), and `~/.local/state/bot-cmder/` (where `audit.jsonl` and `totp.sqlite` materialize on first use).

### From source (contributors)

Requires [`uv`](https://github.com/astral-sh/uv).

```shell
git clone https://github.com/zondatw/bot-cmder.git
cd bot-cmder
uv sync && pre-commit install --install-hooks
uv run bot-cmder init --config-dir .       # scaffolds into repo (./config/, ./var/) instead of ~/.config
uv run bot-cmder serve --reload
```

The from-source flow uses `./config/app.yaml`, `./.env`, and `./var/` (CWD-relative), preserving the Phase 1-7 dev workflow exactly.

### Via Docker / GHCR

```shell
docker pull ghcr.io/zondatw/bot-cmder:latest

# One-time scaffold into a named volume
docker run --rm -it -v bot-cmder-cfg:/etc/bot-cmder \
  ghcr.io/zondatw/bot-cmder:latest init --config-dir /etc/bot-cmder

# Run
docker run -d --name bot-cmder \
  -v bot-cmder-cfg:/etc/bot-cmder:ro \
  -v bot-cmder-state:/var/lib/bot-cmder \
  -p 47823:47823 \
  --restart unless-stopped \
  ghcr.io/zondatw/bot-cmder:latest
```

Multi-arch (amd64 + arm64), <150 MB compressed. Image tags: `latest` /
`0.X.Y` / `beta` / `main`. Full walkthrough including k8s + Compose
examples in [`docs/docker.md`](docs/docker.md).

### Config file locations

`bot-cmder` searches for `app.yaml` (and `.env`, and the state dir) in this order, returning the first hit:

1. `--config <path>` CLI flag / `BOT_CMDER_CONFIG` env var
2. `./config/app.yaml` (CWD-relative, dev workflow)
3. `$XDG_CONFIG_HOME/bot-cmder/app.yaml` (default `~/.config/bot-cmder/app.yaml`, installed flow)

Same precedence for `.env` (`./.env` → `$XDG_CONFIG_HOME/bot-cmder/.env`) and the state dir (`BOT_CMDER_STATE_DIR` → `./var/` → `$XDG_STATE_HOME/bot-cmder/`).

### Required environment

Whatever path resolution lands on, the `.env` (or shell env) needs at minimum:

```
# Phase 2 — required for /kubectl, /runbook run and any Risk.PRIVILEGED command
BOT_CMDER_MASTER_KEY=<run `bot-cmder gen-master-key` to generate>

# At least one platform — Telegram / Discord / Slack
TELEGRAM_TOKEN=xxxxxxxxxxxxx
TELEGRAM_WEBHOOK_SECRET=any-long-random-string
```

## Run

```shell
bot-cmder serve                # PyPI flow
uv run bot-cmder serve --reload  # source flow with autoreload
# or equivalently
python -m bot_cmder serve
```

Defaults to `127.0.0.1:47823` (intentionally uncommon to avoid clashing with other services on 8000 / 8080). Override via `--host` / `--port` flags, or `BOT_CMDER_HOST` / `BOT_CMDER_PORT` / `BOT_CMDER_RELOAD` env vars.

The legacy `BIND_HOST` / `BIND_PORT` / `RELOAD` / `APP_CONFIG_PATH` env names keep working through 0.2.x with a deprecation warning; rename to the `BOT_CMDER_*` form before 0.3.0.

### No public URL? Use the no-domain modes

If your network won't let you expose `https://...` to the internet
(home lab, NAT, restrictive corp egress), each platform has an
outbound-only ingestion mode that needs no public URL or tunnel.
Same dispatcher, same `/cmd` UX, same audit log; only the
ingestion path differs.

```shell
# Telegram — long-poll getUpdates (Phase 6a)
echo "TELEGRAM_MODE=polling" >> .env

# Slack — Socket Mode WebSocket (Phase 6b)
echo "SLACK_MODE=socket"        >> .env
echo "SLACK_APP_TOKEN=xapp-..." >> .env

# Discord — Gateway WebSocket (Phase 6c)
# ⚠️ Trade-off: slash commands don't work in this mode (Discord
# platform limitation). UX becomes `@bot cmd args` or DM `cmd args`.
# Also requires enabling the MESSAGE_CONTENT privileged intent in
# the dev portal: Bot → Privileged Gateway Intents.
echo "DISCORD_MODE=gateway" >> .env

uv run bot-cmder serve
# Logs:
#   telegram adapter mounted (mode=polling)
#   slack adapter mounted (mode=socket, ...)
#   slack socket: hello (connections=1)
#   discord adapter mounted (mode=gateway, ...)
#   discord gateway: READY (bot_id=..., session=...)
```

Full walkthroughs (trade-offs, protocol details, troubleshooting):
- Telegram polling: [`docs/telegram-polling.md`](docs/telegram-polling.md)
- Slack Socket Mode: [`docs/slack-socket-mode.md`](docs/slack-socket-mode.md)
- Discord Gateway: [`docs/discord-gateway.md`](docs/discord-gateway.md)

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
just enroll-totp telegram:1234567890

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
# 1. dev portal → New Application; grab the values:
#    https://discord.com/developers/applications
echo "DISCORD_APPLICATION_ID=..." >> .env  # General Info → Application ID
echo "DISCORD_PUBLIC_KEY=..."     >> .env  # General Info → Public Key
echo "DISCORD_BOT_TOKEN=..."      >> .env  # Bot → Reset Token (shown ONCE)
# Optional but recommended for dev (instant slash command propagation):
# right-click your test server → Copy Server ID (Developer Mode must
# be on in client Settings → Advanced).
echo "DISCORD_GUILD_ID=..."       >> .env

# 2. OAuth2 → URL Generator → check `bot` + `applications.commands`,
#    open the generated URL, invite the bot to your test server.

# 3. dev portal → General Info → Interactions Endpoint URL:
#    https://<your-ngrok>.ngrok-free.app/webhooks/discord
#    (just dev must be running first)

# 4. Push the slash command schema. With DISCORD_GUILD_ID set, this
#    scopes to your test server (instant propagation). For prod, run
#    `just register-discord --global` to force a global push.
just register-discord

# 5. In Discord:
/help
/service args:"restart api --host gce"
/otp code:"123456"
```

For the **full walkthrough** — where each value comes from in the dev
portal, OAuth2 permission discussion, troubleshooting table, and
secret rotation steps — see [`docs/discord-setup.md`](docs/discord-setup.md).

## Slack setup (Phase 5)

Quick path:

```shell
# 1. Slack app management → Create New App → From scratch:
#    https://api.slack.com/apps
echo "SLACK_SIGNING_SECRET=..." >> .env  # Basic Information → App Credentials
echo "SLACK_BOT_TOKEN=xoxb-..."  >> .env  # OAuth & Permissions (after install)

# 2. OAuth & Permissions → add `commands` + `chat:write` scopes →
#    "Install to Workspace" → Allow.

# 3. Generate the manifest from the live registry (single source of
#    truth — no hand-maintained manifest file). Paste output into
#    Slack app → Features → App Manifest → Save → accept reinstall.
echo "SLACK_REQUEST_URL=<your-ngrok-domain>" >> .env  # or set NGROK_DOMAIN
just register-slack > /tmp/slack-manifest.yaml

# 4. (Optional) tune reply visibility in config/app.yaml:
cat >> config/app.yaml <<'YAML'
slack:
  reply_visibility: by_risk        # SAFE → in_channel, PRIVILEGED → ephemeral
  visibility_overrides:
    whoami: ephemeral              # don't broadcast user IDs
YAML

# 5. In Slack:
/help
/service restart api --host gce
/otp 123456
```

For the **full walkthrough** — every UI click, scope rationale,
troubleshooting table, secret rotation, and the visibility
config matrix — see [`docs/slack-setup.md`](docs/slack-setup.md).

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

## License

MIT — see [LICENSE](LICENSE).
