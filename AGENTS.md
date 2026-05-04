# AGENTS.md

Operating rules for AI agents (Claude Code, Codex, etc.) working in
this repo. Humans should also follow these for consistency, but the
primary audience is the model.

---

## 🚨 NEVER commit, document, or quote real identifiers

**This rule is non-negotiable.** A previous session leaked the
maintainer's real Telegram / Discord user IDs, Discord application
ID, and reserved ngrok hostname into committed docs and into PR
descriptions. The repo is public; once leaked, those IDs are
permanently in the git history (recoverable from forks even after
`git filter-repo`) and on third-party indexes.

### What counts as a "real identifier"

| Field | Example of REAL (never commit) | Example of OK (placeholder) |
|---|---|---|
| Telegram user ID | `telegram:236353148` | `telegram:1234567890` |
| Discord user ID  | `discord:230258466000863232` | `discord:1111111111111111111` |
| Slack user ID    | `slack:U0XYZ1A2B` (the maintainer's actual workspace ID) | `slack:U0123ABCD` |
| Discord App ID   | `1500279991190032435` | `1500111111111111111` |
| Slack App ID     | the actual A0... shown in dev portal | `A0123ABCD` |
| Reserved ngrok hostname | `coherent-twisty-stowaway.ngrok-free.dev` | `<your-tunnel>.ngrok-free.dev` or `your-reserved-domain.ngrok-free.dev` |
| Bot tokens / signing secrets / TOTP secrets | any value at all | **never appears in repo** — `.env` is gitignored, `<set>`/`<unset>` only in tooling output |

### Where the rule applies (everywhere)

- Source code, tests, fixtures, configuration files
- Markdown docs (README, `docs/`, this file)
- Code comments, docstrings, log format strings
- Commit messages and commit message bodies
- Pull request titles and descriptions
- Issue titles and descriptions
- Justfile recipes / shell scripts (use `<placeholder>` syntax)
- Chat replies the agent surfaces back to the user **about content
  destined for the repo** (it's fine to paste real values into a
  chat reply when answering "what value should I use" — but those
  same values must be redacted before they reach any committed
  artifact)

### When you NEED to refer to a real value (e.g. dogfood diagnostics)

Use a **partial-mask** format that preserves enough to confirm a
match but not enough to enable abuse:

```
telegram:236...148   ← first 3 + last 3 of the digits
discord:230...232
slack:U0X...A2B
```

This format is acceptable in **chat replies and PR comments** as
diagnostic context. It is **not** acceptable in committed files
(use the placeholder examples instead).

### How to enforce

If the agent (you) is about to write a string that looks like a
real identifier — i.e., it matches one of these patterns AND it's
NOT a documented placeholder from the table above — STOP, redact
to a placeholder, and proceed.

If the agent has already written a real identifier and notices
mid-edit, the correct response is:

1. Immediately fix the file to use a placeholder
2. Tell the user (don't try to hide the slip)
3. Do NOT commit until the fix is in place
4. If the value already shipped to a remote (push, PR), recommend
   the same `git filter-repo` + force-push remediation that produced
   this rule in the first place

There is no exception for "just this one example, the value is
already public elsewhere". Even semi-public values (Discord App ID
appears in OAuth invite URLs anyway) tied to a specific user
constitute fingerprintable leakage.

---

## Other operating rules

### Workflow

- Atomic commits, PR-then-merge always — never push directly to main
  (history rewrite for the leak above was the documented exception)
- `pre-commit run --all-files` must pass before push
- `uv run pytest` must pass before push
- One logical change per commit — split into multiple commits if
  the diff covers unrelated concerns
- Use `git commit -F <file>` for multi-line commit messages —
  heredoc-in-shell-via-bash-tool is fragile

### Tooling

- `uv` for Python package management (not poetry, pip, or pipenv)
- `pre-commit` runs black + ruff + codespell + standard hooks
- Tests live in `tests/`, mirroring `bot_cmder/` structure
- New features ship with tests in the same PR

### Database / persistence

- Every SQLite store uses `bot_cmder.storage.migrator` — never
  inline `CREATE TABLE` in production code
- Migrations are append-only, numbered files in `migrations/`

### Documentation

- New env vars: add to `.env.example` AND `scripts/show_env_settings.py`
  AND the relevant `docs/<feature>.md` AND the README quickstart
- New per-platform setup steps: walkthrough in `docs/<platform>-setup.md`,
  cross-link from README
- New features get a "what's in this release" entry in README

### Audit log invariants (already-pinned tests cover these)

- Every audit event must include a `platform` field
- `/otp` (and any other future credential-bearing command) must
  pass `args` through `bot_cmder.core.redact.redact_args_for_audit`
  before logging — never log the raw OTP code or enrollment URI
- See `bot_cmder/core/redact.py` for the centralized redaction policy
