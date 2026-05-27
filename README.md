# bot-cmder

[![CI](https://github.com/zondatw/bot-cmder/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/zondatw/bot-cmder/actions/workflows/ci.yml)

Multi-platform SRE ChatOps bot — drive maintenance operations from Telegram, Discord, and Slack when you're away from your laptop and prod has issues. Same dispatcher, same `/cmd` UX, same JSONL audit log on every platform; per-message TOTP gate on PRIVILEGED operations so a leaked chat session can't restart your services.

> [!IMPORTANT]
> **`pip install bot-cmder` is not live yet.** The PyPI package hasn't been published — a maintainer account-recovery is in progress (forgotten password, awaiting PyPI support). Until it clears, install [from source](#from-source-contributors) or via the [Docker image](#via-docker--ghcr). This notice will be removed once the first release lands on PyPI.

## Why bot-cmder

- **Operate prod from your phone.** SSH-driven service actions, kubectl, custom runbooks — all reachable through chat. No laptop required during incidents.
- **Strong defaults you can live with.** TOTP-gated PRIVILEGED commands, append-only JSONL audit log with [built-in rotation](docs/audit-rotation.md), per-host SSH command allowlists, ACL-driven access. Safe out of the box.
- **One codebase, three platforms.** Telegram + Discord + Slack adapters share the dispatcher / OTP gate / audit log. Pick one, all three, or none — every adapter mounts on demand.

## Quick start

> Until `pip install bot-cmder` is live (see notice above), swap step 1 for a [source](#from-source-contributors) or [Docker](#via-docker--ghcr) install — the `init` / `enroll-totp` / `serve` steps are identical.

```shell
pip install bot-cmder
bot-cmder init                              # scaffold ~/.config/bot-cmder/{app.yaml,.env} + state dir
# edit ~/.config/bot-cmder/app.yaml — add your users + ACL
bot-cmder enroll-totp --user telegram:<your-id>
bot-cmder serve
```

→ For the full 30-minute walkthrough from `pip install` to "I just ran a privileged command via TOTP", see **[`docs/getting-started.md`](docs/getting-started.md)**.

Other install paths (source / Docker) below.

## Install

### From PyPI (recommended)

> [!NOTE]
> Not published yet — see the notice at the top. Use [from source](#from-source-contributors) or [Docker](#via-docker--ghcr) in the meantime.

```shell
pip install bot-cmder
```

Lands `bot-cmder` on your PATH; pairs with `python -m bot_cmder` (equivalent).

### From source (contributors)

Requires [`uv`](https://github.com/astral-sh/uv).

```shell
git clone https://github.com/zondatw/bot-cmder.git
cd bot-cmder
uv sync && pre-commit install --install-hooks
uv run bot-cmder init --config-dir .       # scaffolds into repo (./config/, ./var/) instead of ~/.config
uv run bot-cmder serve --reload
```

The from-source flow uses `./config/app.yaml`, `./.env`, and `./var/` (CWD-relative), preserving the dev workflow exactly.

### Via Docker / GHCR

```shell
docker pull ghcr.io/zondatw/bot-cmder:latest

docker run --rm -it -v bot-cmder-cfg:/etc/bot-cmder \
  ghcr.io/zondatw/bot-cmder:latest init --config-dir /etc/bot-cmder

docker run -d --name bot-cmder \
  -v bot-cmder-cfg:/etc/bot-cmder:ro \
  -v bot-cmder-state:/var/lib/bot-cmder \
  -p 47823:47823 \
  --restart unless-stopped \
  ghcr.io/zondatw/bot-cmder:latest
```

Multi-arch (amd64 + arm64). Full walkthrough including k8s + Compose examples in [`docs/docker.md`](docs/docker.md).

### Config file locations

`bot-cmder` searches for `app.yaml` (and `.env`, and the state dir) in this order, returning the first hit:

1. `--config <path>` CLI flag / `BOT_CMDER_CONFIG` env var
2. `./config/app.yaml` (CWD-relative, dev workflow)
3. `$XDG_CONFIG_HOME/bot-cmder/app.yaml` (default `~/.config/bot-cmder/app.yaml`, installed flow)

Same precedence for `.env` (`./.env` → `$XDG_CONFIG_HOME/bot-cmder/.env`) and the state dir (`BOT_CMDER_STATE_DIR` → `./var/` → `$XDG_STATE_HOME/bot-cmder/`).

The legacy `BIND_HOST` / `BIND_PORT` / `RELOAD` / `APP_CONFIG_PATH` env names keep working through 0.2.x with a deprecation warning; rename to the `BOT_CMDER_*` form before 0.3.0.

## Documentation

| | |
|---|---|
| **[Getting started](docs/getting-started.md)** | 30-minute walkthrough — from `pip install` to your first PRIVILEGED command |
| **[Full config reference](bot_cmder/data/app.yaml.example)** | Every settable field with default + comment |
| **[CHANGELOG](CHANGELOG.md)** | Per-release notes (Keep a Changelog format) |
| Discord setup | [`docs/discord-setup.md`](docs/discord-setup.md) — app + slash commands |
| Slack setup | [`docs/slack-setup.md`](docs/slack-setup.md) — manifest + signing secret |
| Telegram polling (no domain) | [`docs/telegram-polling.md`](docs/telegram-polling.md) |
| Slack Socket Mode (no domain) | [`docs/slack-socket-mode.md`](docs/slack-socket-mode.md) |
| Discord Gateway (no domain) | [`docs/discord-gateway.md`](docs/discord-gateway.md) |
| TOTP + emergency mode | [`docs/otp.md`](docs/otp.md) |
| Audit log rotation | [`docs/audit-rotation.md`](docs/audit-rotation.md) |
| Docker / GHCR | [`docs/docker.md`](docs/docker.md) |
| Maintainer release procedure | [`docs/release.md`](docs/release.md) |
| Contributor workflow | [`AGENTS.md`](AGENTS.md) — issue-first, atomic commits, PR-then-merge |
| Personal-ID leak prevention | [`docs/git-leak-prevention.md`](docs/git-leak-prevention.md) |

## Architecture (one paragraph)

`bot_cmder/` package layout: `adapters/` (how chat platforms talk to the bot — Telegram, Discord, Slack, with both push and outbound-pull modes per platform); `core/` (registry, dispatcher, parser, ACL, OTP gate); `connectors/` (how the bot talks to infra — local subprocess, SSH); `auth/` (TOTP enrollment, secret store, pending sessions, emergency-bypass windows); `audit/` (JSONL writer with rotation); `config/` (pydantic schema + XDG-aware path resolution); `commands/` (built-in `/help` / `/whoami` / `/health` / `/kubectl` / `/runbook` / `/service` / `/ssh` / `/otp`); `cli/` (the `bot-cmder` shell command). Tests mirror the package layout under `tests/`.

## Run tests

```shell
uv run pytest -q
uv run pre-commit run --all-files
```

## Changelog

Per-release notes — what's added / changed / deprecated / removed — in [CHANGELOG.md](CHANGELOG.md). Format: [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).

## License

MIT — see [LICENSE](LICENSE).
