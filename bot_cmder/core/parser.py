from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    args: list[str]


def parse(text: str) -> ParsedCommand | None:
    """Parse '/cmd arg1 "two words"' into ParsedCommand(name, args).

    Returns None when text is empty, doesn't begin with '/', or shlex fails.
    Strips Telegram's '@botname' suffix on the command head.
    """
    text = text.strip()
    if not text or not text.startswith("/"):
        return None
    try:
        tokens = shlex.split(text)
    except ValueError:
        return None
    if not tokens:
        return None
    head = tokens[0].removeprefix("/")
    if "@" in head:
        head = head.split("@", 1)[0]
    if not head:
        return None
    return ParsedCommand(name=head, args=tokens[1:])
