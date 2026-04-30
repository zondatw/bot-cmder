# bot-cmder

Multi-platform SRE ChatOps bot — drive maintenance operations from Telegram (Discord and Slack land in later phases) when you are away from a computer and prod has issues.

## What's in this release (Phase 1)

- FastAPI service exposing `POST /webhooks/telegram` and `GET /healthz`.
- Telegram adapter on `httpx` (no URL-encoding bugs from the legacy demo).
- `CommandRegistry` + decorator-style registration; `Dispatcher` with structured JSONL audit log.
- ACL via YAML config: per-command allow lists and `role:<name>` expansion.
- Builtins: `/help`, `/whoami`, `/health [target ...]`.
- Connector layer (`LocalConnector`) ready for Phase 2's `kubectl` / `runbook` builtins and Phase 3's `SshConnector`.

TOTP-gated privileged commands (`/kubectl`, `/runbook`), the SSH connector + `/service` actions, and the Discord / Slack adapters arrive in subsequent phases.

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

Local dev binds `127.0.0.1:8000` by default. Override with `BIND_HOST`, `BIND_PORT`, or `RELOAD=1` for autoreload.

## Justfile shortcuts

```shell
just                                  # list tasks
just dev                              # uvicorn with reload
just test                             # pytest
just lint                             # ruff + black --check
just show-env-settings                # echo env vars
just set-telegram-bot-webhook         # POST setWebhook to Telegram
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
