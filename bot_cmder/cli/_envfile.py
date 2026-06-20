"""Tiny .env parser + writer with comment / order preservation (issue #45).

Used by `bot-cmder configure` to surgically update specific keys in
the operator's `.env` without disturbing:

  - The `BOT_CMDER_MASTER_KEY` line (we refuse to write it; it's
    `bot-cmder init`'s sole responsibility)
  - Other adapters' values (configure may run for telegram only;
    discord/slack values must round-trip byte-identical)
  - Comments, blank lines, and the file's section structure
  - File mode (always 0o600 on save, matches init's chmod)

Why not python-dotenv's `set_key`: it's only a transitive dep of
pydantic-settings, not a direct one — relying on it would couple us
to pydantic-settings' resolver choices. It also reorders comments
around the target key in ways we want to control here. ~80 LOC of
hand-rolled parser is cheaper than the dep policy debate.

Parsing is intentionally tight:
  - `KEY = value`  → parsed key/value pair
  - `KEY=value`    → same (whitespace around `=` optional)
  - `KEY="..."` / `KEY='...'`  → outer matching quotes stripped from value
  - `# anything`   → comment, preserved verbatim
  - blank          → preserved verbatim
  - anything else  → preserved verbatim, treated as opaque (no key)

No interpolation, no `export`, no multi-line values. The .env files
`bot-cmder` writes are simple enough that this is sufficient.
"""

from __future__ import annotations

import contextlib
import difflib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Strict-ish key=value matcher. Keys must be uppercase / digits / _,
# starting with a letter or _. Matches the de facto .env convention
# `bot-cmder init` writes and what shells expect from `set -a`.
_KV_RE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$")


@dataclass(frozen=True)
class EnvLine:
    """One line of a `.env` file.

    `key`/`value` are set only for parsed assignments; comments,
    blanks, and malformed lines have `key=None` and survive via
    `raw` (which already includes the trailing newline).
    """

    raw: str
    key: str | None = None
    value: str | None = None


@dataclass
class EnvFile:
    """In-memory model of a `.env` file. Use `EnvFile.load(path)` to
    read; `set()` to mutate; `save()` to write atomically.

    Field order is preserved exactly. Mutation of an existing key
    rewrites only that line. Setting a brand-new key appends to EOF
    under a `# --- added by bot-cmder configure ---` header so the
    diff is obvious in code review.
    """

    path: Path
    lines: list[EnvLine] = field(default_factory=list)

    # Sentinel inserted on first new-key append. Subsequent new keys
    # land below the same header (we don't re-print it).
    _APPENDED_HEADER: str = field(
        default="\n# --- added by `bot-cmder configure` ---\n",
        repr=False,
    )

    # --- load / save -------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> EnvFile:
        """Parse `path` into an EnvFile. Returns an empty EnvFile when
        the file doesn't exist; callers that require existence should
        check before calling."""
        if not path.is_file():
            return cls(path=path, lines=[])
        text = path.read_text(encoding="utf-8")
        # `splitlines(keepends=True)` so we round-trip the original
        # newline style (\n vs \r\n) without intermediate normalization.
        lines: list[EnvLine] = []
        for raw in text.splitlines(keepends=True):
            m = _KV_RE.match(raw.rstrip("\r\n"))
            if m is None:
                lines.append(EnvLine(raw=raw))
                continue
            key, value = m.group(1), m.group(2).strip()
            value = _unquote(value)
            lines.append(EnvLine(raw=raw, key=key, value=value))
        return cls(path=path, lines=lines)

    def save(self, mode: int = 0o600) -> None:
        """Atomically write the in-memory state back to disk. tempfile
        in the same directory + `os.replace` so a crash mid-write
        can't leave a half-written `.env`."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=".env.",
            suffix=".tmp",
            delete=False,
        )
        try:
            tmp.write("".join(line.raw for line in self.lines))
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.chmod(tmp.name, mode)
            os.replace(tmp.name, self.path)
        except Exception:
            # Best-effort cleanup of the temp file on any failure
            # (rename failed, fsync failed, etc.); the original
            # `.env` is untouched thanks to the rename-not-overwrite
            # contract.
            with contextlib.suppress(OSError):
                os.unlink(tmp.name)
            raise

    # --- read / write ------------------------------------------------

    def get(self, key: str) -> str | None:
        """Latest value for `key`, or None if absent. If somehow the
        file has the key declared more than once (legal under
        most .env interpreters), the LAST one wins — matches the
        dotenv / shell `set -a` semantics."""
        result: str | None = None
        for line in self.lines:
            if line.key == key:
                result = line.value
        return result

    def keys(self) -> list[str]:
        """All keys declared in the file, in declaration order. May
        contain duplicates if the file does."""
        return [line.key for line in self.lines if line.key is not None]

    def set(self, key: str, value: str | None) -> None:
        """Set or clear `key`.

        - `value` is a string → write `KEY=value`. Existing line
          updated in place; if key is new, append below the
          `# --- added by ... ---` header (added on first append).
        - `value=None` → write `KEY=` (empty value, comment above
          the line preserved). Use this when an operator picks
          `[Clear]` for an existing key — leaves the placeholder
          context intact for a future re-edit.

        Always sets the value as bare (unquoted) text. Existing
        quoting on the line is replaced — we don't try to preserve
        whether the operator originally wrote `KEY="foo"` vs
        `KEY=foo`; both round-trip to `KEY=foo` after a set().
        """
        new_value = "" if value is None else value
        new_raw = f"{key}={new_value}\n"

        # Update the LAST matching line. `get()` returns last-wins
        # (matches dotenv / shell `set -a` semantics), so if we
        # touched the first match instead, a later duplicate would
        # silently override our write and the wizard's set would be
        # invisible to both `get()` and the runtime. The earlier
        # duplicate is left as dead code rather than stripped — the
        # operator may want it for context when re-editing by hand.
        last_idx = None
        for i, line in enumerate(self.lines):
            if line.key == key:
                last_idx = i
        if last_idx is not None:
            self.lines[last_idx] = EnvLine(raw=new_raw, key=key, value=new_value)
            return

        # New key — append, with the header on the first new append
        # only.
        if not self._has_append_header():
            self.lines.append(EnvLine(raw=self._APPENDED_HEADER))
        self.lines.append(EnvLine(raw=new_raw, key=key, value=new_value))

    def _has_append_header(self) -> bool:
        """True if the "added by configure" sentinel header is
        already present (so we don't double-print it on subsequent
        new-key sets in the same session)."""
        marker = "added by `bot-cmder configure`"
        return any(marker in line.raw for line in self.lines)

    # --- diff (for --dry-run) ----------------------------------------

    def diff(self, original: EnvFile) -> str:
        """Unified diff between `original` and self. Empty string if
        no changes — callers can use that as a "nothing to write"
        signal."""
        a = "".join(line.raw for line in original.lines)
        b = "".join(line.raw for line in self.lines)
        if a == b:
            return ""
        return "".join(
            difflib.unified_diff(
                a.splitlines(keepends=True),
                b.splitlines(keepends=True),
                fromfile=str(self.path) + " (current)",
                tofile=str(self.path) + " (proposed)",
                n=3,
            )
        )


def _unquote(value: str) -> str:
    """Strip ONE pair of matching outer quotes if present. Inner
    quotes are left alone — they're part of the value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value
