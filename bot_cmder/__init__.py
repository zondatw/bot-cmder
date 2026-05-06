"""bot-cmder — multi-platform SRE ChatOps bot."""

from __future__ import annotations

# Single source of truth for the package version. `pyproject.toml`
# reads this via `[tool.hatch.version] path = "bot_cmder/__init__.py"`,
# `bot_cmder/main.py` passes it to FastAPI's OpenAPI metadata, and the
# CLI dispatcher prints it for `bot-cmder --version`. Bump here before
# tagging a release; everything downstream picks up automatically.
__version__ = "0.2.0"
