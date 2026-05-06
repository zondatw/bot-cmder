# Cutting a release

Maintainer-facing playbook for publishing `bot-cmder` to PyPI.
Pairs with the GitHub Actions workflows in
[`.github/workflows/beta.yml`](../.github/workflows/beta.yml) and
[`.github/workflows/release.yml`](../.github/workflows/release.yml).

The release pipeline is **branch-based**, mirroring
[`zondatw/remote-cmder`](https://github.com/zondatw/remote-cmder)'s
pattern:

```
main          ← regular dev, every PR lands here
  ├──→ beta       (push triggers test.pypi.org publish)
  └──→ release    (push triggers pypi.org publish)
```

Each branch's HEAD is the canonical "what's currently on this index?"
— `git log beta` answers test.pypi.org, `git log release` answers
prod PyPI. No tag dance required for a basic release; tag manually
afterwards if you want archeological landmarks.

## TL;DR (regular release after first-time setup)

```bash
# 1. Bump the single source of truth on main
$EDITOR bot_cmder/__init__.py    # bump __version__
git commit -am "release: 0.X.Y"
git push                         # lands on main via PR

# 2. Forward to beta — fires beta.yml → test.pypi.org
git checkout beta
git merge --ff-only main
git push

# 3. Verify on test.pypi.org. README rendering, license badge,
#    install command. In a fresh venv:
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ \
            bot-cmder
bot-cmder --version

# 4. If clean, forward to release — fires release.yml → pypi.org
git checkout release
git merge --ff-only beta
git push

# 5. Approve the publish step in the `release` environment when
#    prompted (if required reviewers are configured), then verify:
open https://pypi.org/project/bot-cmder/
```

That's it for an established release. The first one needs one-time
setup — see the next section.

---

## One-time setup (do this once before the first publish)

### 1. PyPI accounts

If you don't already have them:

- https://pypi.org/account/register/
- https://test.pypi.org/account/register/ (separate account, separate
  password, separate 2FA enrollment — the two indices are unrelated
  identity systems)

Verify both emails. Enable 2FA on prod PyPI (mandatory for uploads
since 2024).

### 2. PyPI Trusted Publishing — pending publisher (prod)

Because `bot-cmder` doesn't exist on PyPI yet, use the *pending*
publisher form, not the normal one (which needs an existing project).

1. Open https://pypi.org/manage/account/publishing/
2. Scroll to "Add a new pending publisher". Fill **exactly**:
   - **PyPI Project Name**: `bot-cmder`
   - **Owner**: `zondatw`
   - **Repository name**: `bot-cmder`
   - **Workflow name**: `release.yml`
   - **Environment name**: `release`
3. Submit. PyPI now trusts a future workflow run from
   `zondatw/bot-cmder` whose `release.yml` enters the `release`
   environment.

### 3. Test PyPI Trusted Publishing — pending publisher

Repeat for test.pypi.org. **Different workflow + environment names**:

1. Open https://test.pypi.org/manage/account/publishing/
2. "Add a new pending publisher". Fill:
   - **PyPI Project Name**: `bot-cmder`
   - **Owner**: `zondatw`
   - **Repository name**: `bot-cmder`
   - **Workflow name**: `beta.yml`
   - **Environment name**: `release-test`

### 4. GitHub environments

Both workflows reference environments that need to exist for the
OIDC token gating to work:

1. https://github.com/zondatw/bot-cmder/settings/environments
2. "New environment" → name `release` → Save.
   - Recommended: under "Deployment protection rules" → "Required
     reviewers" → add yourself. Each push to the `release` branch
     then pauses at the publish step until you click "Approve" in
     the run page. Cheap insurance against an accidental push.
3. Repeat for `release-test`. Required reviewers are less critical
   here (worst case: a junk version on test.pypi.org), but keep the
   environments parallel for clarity.

### 5. Create the `beta` and `release` branches

```bash
git checkout -b beta main
git push -u origin beta

git checkout -b release main
git push -u origin release
```

### 6. First test.pypi.org publish (validates the whole pipeline)

Do this once before the first prod publish, to shake out any
config mistakes on test.pypi.org first (where a botched version
number is harmless).

After PR-A merges (issue #20) and `__version__` is at `0.2.0`:

```bash
git checkout beta
git merge --ff-only main
git push
```

Watch the workflow at
https://github.com/zondatw/bot-cmder/actions/workflows/beta.yml.
Approve the `release-test` environment when prompted.

After publish, verify on https://test.pypi.org/project/bot-cmder/:

- README renders correctly (PyPI uses a markdown subset; watch for
  un-rendered tables or relative-link images)
- License shows `MIT`
- "Project links" sidebar lists Homepage / Repository / Issues
  (sourced from `[project.urls]` in pyproject.toml)
- Installable in a fresh venv:

  ```bash
  python3 -m venv /tmp/smoke
  /tmp/smoke/bin/pip install --index-url https://test.pypi.org/simple/ \
                              --extra-index-url https://pypi.org/simple/ \
                              bot-cmder
  /tmp/smoke/bin/bot-cmder --version  # should print bot-cmder 0.2.0
  /tmp/smoke/bin/bot-cmder init --config-dir /tmp/smoke-cfg
  ```

If anything looks off, fix on a normal PR, merge to main, fast-
forward `beta` again. Test PyPI rejects re-uploading the same
version literal — bump `__version__` (e.g. to `0.2.0a1`) before
re-pushing if you need to retry without changing the real version
target.

### 7. First prod publish

After test.pypi.org is green:

1. `__version__` should be back to the real target (e.g. `0.2.0`,
   not `0.2.0a1` if you used a suffix during testing).
2. Forward to release:
   ```bash
   git checkout release
   git merge --ff-only beta
   git push
   ```
3. Approve the `release` environment when prompted at
   https://github.com/zondatw/bot-cmder/actions/workflows/release.yml.
4. Verify https://pypi.org/project/bot-cmder/ shows the new version.
5. Optional: tag for archeology + draft a GitHub release.
   ```bash
   git tag -a v0.2.0 -m "v0.2.0 — first PyPI publish"
   git push --tags
   ```
   Then https://github.com/zondatw/bot-cmder/releases/new?tag=v0.2.0
   to draft notes.

---

## What can go wrong

| Symptom | Cause | Fix |
|---|---|---|
| Publish step fails with `403`, mentions trusted publishing | Pending publisher misconfigured: typo in owner / repo / workflow / environment fields, OR environment doesn't exist on the GitHub side | Compare the four fields in the PyPI publishing page against the workflow's `name:` and `environment.name`. The four must match **exactly** including case. |
| Build step fails at "Verify wheel ships expected data files" | A non-`.py` file dropped from the wheel | Run `uv build && unzip -l dist/bot_cmder-*.whl \| grep -E '(app\.yaml\.example\|0001_initial\.sql)'` locally; check `[tool.hatch.build.targets.wheel]` in pyproject.toml. |
| Workflow succeeds but PyPI page shows the previous version | `__version__` not bumped before pushing to beta/release | Bump on main, fast-forward beta + release. PyPI will reject a re-upload of the same version literal so this manifests as a 400 error rather than a silent "wrong version" — the `release` page just stays at the prior good version. |
| Push to beta/release rejected with "non-fast-forward" | beta/release diverged from main (someone hot-fixed directly on the branch) | Reconcile manually. Easiest: `git checkout main`, `git merge beta` to pull the divergent commits home, resolve conflicts, then forward main → beta → release as normal. |
| Test PyPI publish 400s with "File already exists" | PyPI rejects re-uploading the same version literal | Bump `__version__` on main (e.g. add `a1` suffix), forward to beta. Each push must publish a distinct version. |
| Push to beta/release didn't fire the workflow | Direct push to a protected branch was blocked, OR the workflow file isn't on that branch yet | First push of a new workflow file: it fires from the branch it lives on. If `beta.yml` is on `main` only and you push to `beta`, the workflow doesn't exist on `beta` yet. Easiest fix: cherry-pick or fast-forward the workflow files onto each release branch first. |

---

## Why no auto-version-bump

The pipeline deliberately does NOT auto-bump `__version__` from a
git tag or branch metadata. Two reasons:

1. **Single source of truth**: `bot_cmder/__init__.py:__version__` is
   what the running CLI reports as `--version`, what FastAPI's
   OpenAPI metadata claims, and what the wheel's
   Metadata-Version field publishes. Auto-bumping it during build
   would mean dev installs from a fresh clone show stale values
   until they push something.

2. **Mismatch is loud, not silent**: forgetting to bump produces a
   visible `400 File already exists` on PyPI's side. Auto-bumping
   would publish silently with whatever version got computed, which
   is a worse failure mode.

If the manual bump ever becomes friction, candidates:
- A pre-publish `validate` job that asserts the
  `__version__` doesn't already exist on the target index, with a
  clear error message ("bump bot_cmder/__init__.py before pushing
  to <branch>")
- `hatch-vcs` (version derived from the latest git tag at build
  time) — drops the bump step entirely but produces ugly local
  versions like `0.2.0.dev3+g7a2b1cd.d20260506` during dev

Until then: bump on main, then fast-forward beta + release.
