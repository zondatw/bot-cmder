"""XDG-aware path resolution for config + state directories.

Helpers used by `bot_cmder.config.settings.Settings.resolve_*`,
`bot_cmder.config.schema.{AuditConfig,TOTPConfig}` defaults, and the
`bot-cmder init` CLI.

The motivating problem: pre-issue-#20 every default was hardcoded
relative to CWD (`./var/audit.jsonl`, `./config/app.yaml`). That works
for `cd ~/dev/bot-cmder && uv run bot-cmder serve`, but it breaks for
the post-issue-#20 PyPI flow where users `pip install bot-cmder` and
run from anywhere — there's no `./var` in their CWD.

Resolution order (decided in chat 2026-05-06):

    state_dir() / config_dir():
        1. explicit env var (BOT_CMDER_STATE_DIR / BOT_CMDER_CONFIG)
        2. ./var/  /  ./config/  (CWD, only if it already exists —
           preserves the dev workflow exactly)
        3. $XDG_STATE_HOME/bot-cmder/  /  $XDG_CONFIG_HOME/bot-cmder/
           (defaulting to ~/.local/state/bot-cmder/  /  ~/.config/bot-cmder/)

CWD wins over XDG when present so `cd ~/dev/bot-cmder && bot-cmder
serve` keeps using `./var/`, `./config/app.yaml`, `./.env` exactly
like Phase 1-7. Installed users get XDG. No surprise for either.
"""

from __future__ import annotations

import os
from pathlib import Path

# Module name used for both XDG_STATE_HOME and XDG_CONFIG_HOME
# subdirectories. Keep singular ("bot-cmder", not "bot_cmder") to
# match the project name on PyPI / the `bot-cmder` console script.
_APP_DIRNAME = "bot-cmder"


def _xdg_dir(env_var: str, default_subpath: tuple[str, ...]) -> Path:
    """Resolve an XDG base dir + the bot-cmder subdir.

    Returns `$<env_var>/bot-cmder` if set; otherwise `~/<*default_subpath>/bot-cmder`.
    """
    base = os.environ.get(env_var)
    if base:
        return Path(base) / _APP_DIRNAME
    return Path.home().joinpath(*default_subpath, _APP_DIRNAME)


def state_dir() -> Path:
    """Where bot-cmder writes mutable state — audit.jsonl, totp.sqlite.

    Resolution order:
      1. BOT_CMDER_STATE_DIR env var (always wins; doesn't need to exist
         yet — first write creates it)
      2. ./var/ (CWD-relative, ONLY if already a directory — keeps the
         dev workflow identical for source contributors)
      3. $XDG_STATE_HOME/bot-cmder/ (default ~/.local/state/bot-cmder/)
    """
    explicit = os.environ.get("BOT_CMDER_STATE_DIR")
    if explicit:
        return Path(explicit)
    cwd_var = Path("./var").resolve()
    if cwd_var.is_dir():
        return cwd_var
    return _xdg_dir("XDG_STATE_HOME", (".local", "state"))


def config_dir() -> Path:
    """Where bot-cmder reads operator config — app.yaml, .env.

    Resolution order:
      1. BOT_CMDER_CONFIG_DIR env var
      2. ./config/ (CWD-relative, ONLY if already a directory)
      3. $XDG_CONFIG_HOME/bot-cmder/ (default ~/.config/bot-cmder/)

    Note this returns the *directory*, not the path to a specific file.
    Callers append `app.yaml` / `.env` themselves.
    """
    explicit = os.environ.get("BOT_CMDER_CONFIG_DIR")
    if explicit:
        return Path(explicit)
    cwd_config = Path("./config").resolve()
    if cwd_config.is_dir():
        return cwd_config
    return _xdg_dir("XDG_CONFIG_HOME", (".config",))


def env_file_path() -> Path | None:
    """Locate the `.env` file pydantic-settings should load.

    Order:
      1. ./.env (CWD, only if it exists)
      2. <config_dir()>/.env (if it exists)
      3. None — pydantic-settings handles None as 'no env file', not an error

    Returns None rather than a non-existent path so pydantic-settings
    doesn't log a misleading "couldn't read env file" warning when the
    user simply hasn't created one yet.
    """
    cwd_env = Path("./.env").resolve()
    if cwd_env.is_file():
        return cwd_env
    xdg_env = config_dir() / ".env"
    if xdg_env.is_file():
        return xdg_env
    return None
