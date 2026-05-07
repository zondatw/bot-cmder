# Changelog

All notable changes to `bot-cmder` are recorded here.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per-release maintainer note: every PR with user-visible behavior adds a
line under `## [Unreleased]` in the same commit. At release time, that
section gets renamed to `## [0.X.Y] - YYYY-MM-DD` (see
[`docs/release.md`](docs/release.md) for the full procedure).

## [Unreleased]

## [0.2.0] - 2026-05-07

First PyPI release. The bot is now `pip install bot-cmder`-able plus a
container image at `ghcr.io/zondatw/bot-cmder:0.2.0`.

### Added

- `bot-cmder` shell command with subcommands: `serve`, `init`,
  `gen-master-key`, `enroll-totp`, `list-totp`, `revoke-totp`,
  `discord-register`, `slack-manifest` ([#22], closes [#20]).
- `python -m bot_cmder` entry point, equivalent to the shell command ([#22]).
- `bot-cmder init` — first-run scaffold creating `app.yaml` + `.env`
  (with a freshly generated `BOT_CMDER_MASTER_KEY`, chmod 600) + state
  directory. `--force` preserves the existing master key by default
  to protect TOTP enrollments; `--rotate-key` opts into regenerating
  ([#22]).
- `BOT_CMDER_CONFIG`, `BOT_CMDER_CONFIG_DIR`, `BOT_CMDER_STATE_DIR`,
  `BOT_CMDER_HOST`, `BOT_CMDER_PORT`, `BOT_CMDER_RELOAD` env vars
  ([#22]).
- XDG-aware config + state discovery (`~/.config/bot-cmder/`,
  `~/.local/state/bot-cmder/`); CWD-relative paths still win when
  present so the dev workflow stays identical ([#22]).
- `[project.scripts]` + dynamic version metadata in `pyproject.toml`,
  with `__version__` in `bot_cmder/__init__.py` as the single source
  of truth ([#22]).
- Multi-arch Docker image (linux/amd64 + linux/arm64) on GHCR;
  branch-based publish workflow (push to `release` → tag `:latest` +
  the version literal) ([#26], closes [#25]).
- [`docs/docker.md`](docs/docker.md) — k8s + Compose deployment
  examples, env-var pass-through, healthcheck behavior, multi-arch
  notes ([#26]).
- PyPI publish workflows (`beta` → test.pypi.org, `release` →
  pypi.org) via Trusted Publishing — no long-lived API tokens
  ([#23]).
- [`docs/release.md`](docs/release.md) — maintainer release procedure
  with one-time PyPI / GHCR setup walkthrough ([#23]).
- CI wheel-build smoke test on every PR — catches packaging
  regressions before they reach `release` ([#24]).
- Emergency OTP-bypass window for incident response (`/otp emergency
  <minutes>`, `/otp end`, `/otp status`) so SREs aren't typing TOTP
  codes for every PRIVILEGED command during an incident
  ([#17], closes [#15]).
- `MIT` LICENSE + `[project.license]` metadata so the wheel + GHCR
  page surface it correctly ([#19]).
- `bot_cmder/data/app.yaml.example` is now a complete reference —
  every settable field shown with its default, sections tagged with
  the originating phase ([#21]).

### Changed

- `audit.path` and `totp.secret_store_path` now resolve to
  `~/.local/state/bot-cmder/...` on installed deployments. CWD
  `./var/` still wins when the directory exists, so source
  contributors keep the Phase 1-7 dev workflow byte-for-byte
  unchanged ([#22]).
- `config/app.yaml.example` moved to `bot_cmder/data/app.yaml.example`
  so it ships in the wheel and `bot-cmder init` can read it via
  `importlib.resources` ([#22]).
- Slack `/otp` reachable via four sub-commands (`/otp code <code>`,
  `/otp emergency <minutes>`, `/otp end`, `/otp status`) instead of a
  single required STRING option — fixes Discord UI making
  `/otp end` and `/otp status` literally unsubmittable
  ([#17], closes [#18]).

### Deprecated

- `BIND_HOST`, `BIND_PORT`, `RELOAD`, `APP_CONFIG_PATH` env var names.
  Use `BOT_CMDER_HOST`, `BOT_CMDER_PORT`, `BOT_CMDER_RELOAD`,
  `BOT_CMDER_CONFIG`. Old names emit a `DeprecationWarning` + log
  warning at startup; will be removed in 0.3.0 ([#22]).

### Removed

- `server.py` (4-line uvicorn wrapper). Use `bot-cmder serve` or
  `python -m bot_cmder serve` ([#22]). **BREAKING** — operators with
  systemd units pointing at `python server.py` need to update one
  line.
- `scripts/register_discord_commands.py` and
  `scripts/register_slack_commands.py`. Use `bot-cmder discord-register`
  / `bot-cmder slack-manifest` ([#22]).

### Security

- `.env` files created by `bot-cmder init` get `chmod 600` so the
  Fernet master key isn't world-readable ([#22]).
- Trusted Publishing replaces long-lived `TWINE_PASSWORD` for the
  PyPI publish — short-lived OIDC token, no rotation required, no
  leak surface in repo secrets ([#23]).

[Unreleased]: https://github.com/zondatw/bot-cmder/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/zondatw/bot-cmder/releases/tag/v0.2.0

[#15]: https://github.com/zondatw/bot-cmder/issues/15
[#17]: https://github.com/zondatw/bot-cmder/pull/17
[#18]: https://github.com/zondatw/bot-cmder/issues/18
[#19]: https://github.com/zondatw/bot-cmder/pull/19
[#20]: https://github.com/zondatw/bot-cmder/issues/20
[#21]: https://github.com/zondatw/bot-cmder/pull/21
[#22]: https://github.com/zondatw/bot-cmder/pull/22
[#23]: https://github.com/zondatw/bot-cmder/pull/23
[#24]: https://github.com/zondatw/bot-cmder/pull/24
[#25]: https://github.com/zondatw/bot-cmder/issues/25
[#26]: https://github.com/zondatw/bot-cmder/pull/26
