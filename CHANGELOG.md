# Changelog

All notable changes to `bot-cmder` are recorded here.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per-release maintainer note: every PR with user-visible behavior adds a
line under `## [Unreleased]` in the same commit. At release time, that
section gets renamed to `## [0.X.Y] - YYYY-MM-DD` (see
[`docs/release.md`](docs/release.md) for the full procedure).

## [Unreleased]

### Added

- OTP brute-force lockout. After `max_failures` `OTP_INVALID` events
  within `failure_window_minutes`, the user gets locked out for
  `lockout_minutes`; while locked, every `/otp <code>` is rejected
  (audit `OTP_LOCKED_OUT`) — even the correct one. Per-norm_id
  scoping (locking `slack:U0X...` doesn't lock `telegram:111`).
  SQLite-backed at `<state_dir>/totp_lockout.sqlite` so state
  survives bot restart. Defaults: 5 failures / 10-min lockout /
  15-min sliding window. Tunable in `app.yaml` under
  `totp.lockout:`. ([#34], closes [#33])
- `bot-cmder unlock-totp --user <norm_id>` admin CLI to clear
  lockout state (logs `OTP_LOCKOUT_ADMIN_RESET` for traceability).
  Use case: SRE locks themselves out at 3am during an incident
  and needs immediate access. ([#34])
- New audit events: `OTP_LOCKOUT_TRIGGERED`, `OTP_LOCKED_OUT`,
  `OTP_LOCKOUT_EXPIRED`, `OTP_LOCKOUT_ADMIN_RESET`. ([#34])
- Per-level ANSI colorized log output in TTY mode (cyan / green /
  yellow / red / bold-red for DEBUG / INFO / WARNING / ERROR /
  CRITICAL) plus dim timestamp + dim logger name. Auto-detected via
  `sys.stderr.isatty()`; honors `NO_COLOR` env (https://no-color.org/).
  Makes ERROR / WARNING easier to spot among INFO noise during
  dev iteration; non-TTY (logfile, pipe, container journal) gets
  the plain formatter unchanged. ([#35])

### Changed

- **BEHAVIOR CHANGE (default-on)**: OTP lockout is enabled by
  default. Existing deployments upgrading to this version
  will start locking out users who fail OTP 5 times in 15 min.
  Set `totp.lockout.enabled: false` in `app.yaml` to opt out
  (audit log still records `OTP_INVALID` per attempt — just no
  lockout fires). ([#34])

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
[#33]: https://github.com/zondatw/bot-cmder/issues/33
[#34]: https://github.com/zondatw/bot-cmder/pull/34
[#35]: https://github.com/zondatw/bot-cmder/pull/35
