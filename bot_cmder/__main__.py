"""`python -m bot_cmder` entry point — delegates to the CLI dispatcher.

Pairs with the console-script `bot-cmder` declared in `pyproject.toml`'s
`[project.scripts]`. Both invocations end up in `bot_cmder.cli.main`,
so `python -m bot_cmder serve` and `bot-cmder serve` are equivalent.
"""

from __future__ import annotations

import sys

from bot_cmder.cli import main

if __name__ == "__main__":
    sys.exit(main())
