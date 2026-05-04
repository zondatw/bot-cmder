# Git leak prevention

Operator guide to the **gitleaks** automation that backs the policy
in [AGENTS.md](../AGENTS.md). Three layers of defense, why they exist,
how to extend them, and how to handle false positives without
weakening the gate.

---

## Why this exists

A previous session leaked the maintainer's real Telegram + Discord
user IDs and reserved ngrok hostname into committed docs. The repo
was public; remediation required:

1. `git filter-repo` to rewrite the dirty commits' content
2. `git push --force` to overwrite remote main
3. Deleting source branches of the affected merged PRs
4. A GitHub Support ticket to purge the dangling commits + PR refs
   (which `filter-repo` can't reach because GitHub keeps them in
   `refs/pull/N/*`)

Total elapsed time: ~3 days (mostly waiting on GitHub Support).

Phase 7 ensures the same class of leak can't recur:

- **AGENTS.md** documents the policy ("never commit real identifiers").
- **`.gitleaks.toml`** encodes the policy as machine-checkable rules.
- **Pre-commit hook** rejects local `git commit` with a leak in the
  staged diff.
- **CI `secret-scan` job** rejects PR merge if pre-commit was bypassed.

Defense in depth: the hook fires fast (immediate feedback during
development), CI is the safety net (catches `--no-verify` and any
contributor without pre-commit installed).

---

## Three layers explained

### Layer 1 — Pre-commit hook (local, fast)

Runs every time you `git commit`. Wired in
[`.pre-commit-config.yaml`](../.pre-commit-config.yaml):

```yaml
- repo: https://github.com/gitleaks/gitleaks
  rev: v8.30.1
  hooks:
    - id: gitleaks
```

Scans the **staged diff** only (not the whole repo), so it's fast
even with hundreds of commits in history. Uses
[`.gitleaks.toml`](../.gitleaks.toml) auto-discovered from the repo
root.

Failure mode:

```
gitleaks.....................................................Failed
- hook id: gitleaks
- exit code: 1

Finding:     telegram:236...148   ← redacted in this doc; the hook
                                    surfaces the actual value
RuleID:      telegram-user-id-prefixed
File:        docs/example.md:42
```

Fix: edit the file, replace with the documented placeholder
(`telegram:1234567890`), re-stage, re-commit. Hook re-runs
automatically.

### Layer 2 — CI `secret-scan` job (remote, safety net)

Runs on every push to `main` and every PR. Wired in
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml):

```yaml
secret-scan:
  steps:
    - uses: actions/checkout@v6
      with:
        fetch-depth: 0          # full history; gitleaks needs it
    - uses: gitleaks/gitleaks-action@v2
```

Scans the **entire commit history of the PR branch**, not just the
diff. Catches:

- Pre-commit hook bypassed via `git commit --no-verify`
- Contributor running an older gitleaks version that didn't have
  the rule yet (rule was added between their commit and the merge)
- Forked PRs from contributors who haven't installed pre-commit

The job is **independent** (not a step in the `lint` job) because
it's a security gate, not a style gate. When PR `secret-scan` fails,
the status check name is unambiguous about what broke; the red badge
visually distinguishes from "pre-commit failed because black wanted
to reformat".

Failure mode: PR shows `secret-scan: failed`. Click into the run
log, see the same `Finding/RuleID/File` triple as the local hook.

### Layer 3 — `just check-leaks` (manual, on demand)

For "I want to know NOW if anything in my working tree would
trip the gate":

```shell
just check-leaks
```

Wraps `gitleaks detect --no-banner --verbose`. Same config and
rules as the hook + CI, so output is byte-equivalent. Useful when:

- Reviewing someone else's PR locally before merging
- Auditing a doc draft before opening the PR
- Investigating a CI failure without round-tripping through GitHub

---

## Adding a new rule

When you discover a NEW class of identifier the project should
never commit (e.g. you start integrating Microsoft Teams and need
to ban `teams:USR123` real values):

1. **Edit AGENTS.md** — add a row to the placeholder table:

   ```markdown
   | Teams user ID | `teams:01a...xyz` (real, redacted) | `teams:0123abcd-...` |
   ```

2. **Edit `.gitleaks.toml`** — add a `[[rules]]` block:

   ```toml
   [[rules]]
   id = "teams-user-id-prefixed"
   description = "Microsoft Teams user ID with 'teams:' prefix"
   regex = '''\bteams:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'''
   tags = ["pii", "bot-cmder"]
   ```

3. **Edit `.gitleaks.toml`** — add the placeholder to allowlist:

   ```toml
   [allowlist]
   regexes = [
       # ... existing entries ...
       '''teams:0123abcd-[0-9a-f-]+''',
   ]
   ```

4. **Validate locally** — write a test file with a real-looking
   value and confirm the rule fires:

   ```shell
   echo "leak: teams:abcd1234-aaaa-bbbb-cccc-deadbeefcafe" > /tmp/probe-dir/test.md
   gitleaks detect --no-git --source /tmp/probe-dir --config $(pwd)/.gitleaks.toml
   ```

   Then write a placeholder version and confirm it passes:

   ```shell
   echo "ok: teams:0123abcd-1111-2222-3333-444444444444" > /tmp/probe-dir/test.md
   gitleaks detect --no-git --source /tmp/probe-dir --config $(pwd)/.gitleaks.toml
   # Expected: "no leaks found"
   ```

5. **Run `just check-leaks`** on the working tree to confirm no
   pre-existing strings in the codebase trigger the new rule.

6. **Commit + push** — the new rule will block subsequent leaks of
   that class.

---

## Handling false positives

A "false positive" is gitleaks flagging a string that LOOKS like a
real identifier but is intentionally a placeholder/example.

### Resolution priorities (best to worst)

#### 1. Use a documented placeholder (BEST)

If the string is in a doc/example file:

- Replace with the placeholder from AGENTS.md's table
  (`telegram:1234567890`, `discord:1111111111111111111`, etc.)
- The allowlist already covers these — no `.gitleaks.toml` change

#### 2. Add to `[allowlist].regexes` (only for additional placeholders)

If you NEED a different placeholder shape (e.g. a fixture that
must use a specific value to test boundary conditions):

```toml
[allowlist]
regexes = [
    # ... existing ...
    '''telegram:1234567890''',  # the canonical placeholder from AGENTS.md
]
```

Keep allowlist entries narrow — `telegram:1234567890` is fine,
`telegram:\d+` (anything) defeats the rule entirely.

#### 3. Add to `[allowlist].paths` (whole-file exempt; use sparingly)

If a whole file legitimately contains many real-looking strings
that aren't covered by individual placeholder regexes (e.g. a
test fixture file with many synthesized IDs):

```toml
[allowlist]
paths = [
    '''^tests/fixtures/realistic_ids\.json$''',
]
```

**Warning**: a path exemption disables ALL rules for that file. Use
only when listing per-string allowlist entries would be unworkable.

#### 4. Bypass the hook (BAD; last resort)

```shell
git commit --no-verify       # locally bypass pre-commit
```

The CI `secret-scan` job will still catch you on push, so this
buys time but doesn't actually let leaked content reach the repo.
**No legitimate workflow requires `--no-verify`** — if you find
yourself reaching for it, fix the rule or the placeholder
allowlist instead.

---

## Bypass policy

| Reason | Allowed? |
|---|---|
| "The hook is too slow" | ❌ Hook runs in < 1s on a normal diff. Slow = false positive blowup, fix the cause |
| "I'll fix it in a follow-up commit" | ❌ Hook will block the follow-up too. Fix in the same commit |
| "It's just a comment / draft" | ❌ Comments are committed too; gitleaks doesn't distinguish |
| "I'm 100% sure it's fake" | ❌ If you're 100% sure, the allowlist will pass it; if it doesn't, your assumption is wrong |
| CI is offline and you must ship a critical fix | ⚠️ Last resort. Document the bypass in the PR description; circle back the moment CI is up |

The CI scan is the canonical gate, not the hook. Bypassing the
hook only delays detection; it doesn't enable a leak.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Hook fails locally but I don't see anything sensitive | gitleaks default rules caught something (entropy-based false positive on random hex) | look at the `RuleID` — if it's `generic-api-key` or similar default, see if your string can be redacted to a less entropy-bearing example |
| `just check-leaks` works locally but CI `secret-scan` fails | rule was added after your branch was created; rebase onto latest main | `git fetch origin main && git rebase origin/main` |
| Hook keeps catching the same placeholder I'm SURE I added to allowlist | regex syntax — gitleaks uses Go regex, not PCRE / Python. Common gotcha: `\d` works but `\d+` greedy matching may overrun | test the regex with `gitleaks detect --config .gitleaks.toml` against an isolated file |
| `just check-leaks` says "no leaks found" but I committed something I shouldn't have | a file in `[allowlist].paths`? `tests/` is fully exempt — review the path list in `.gitleaks.toml` |
| The pre-commit hook isn't running on `git commit` | `pre-commit install` was never run after cloning | `pre-commit install --install-hooks` |

---

## Related docs

- [AGENTS.md](../AGENTS.md) — the policy this enforces
- [.gitleaks.toml](../.gitleaks.toml) — the rule + allowlist config
- [.pre-commit-config.yaml](../.pre-commit-config.yaml) — Layer 1 hook wiring
- [.github/workflows/ci.yml](../.github/workflows/ci.yml) — Layer 2 CI job wiring
