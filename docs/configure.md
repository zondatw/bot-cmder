# `bot-cmder configure` — interactive credential wizard

Walks you through populating `.env` with Telegram / Discord / Slack
credentials after `bot-cmder init` has scaffolded the file. Asks one
question at a time, validates token formats inline, optionally pings
the platform's API to confirm the credential actually works before
writing.

Run after `init`, run again any time you want to add a platform,
flip a mode, or rotate a token. Idempotent — picking "Keep current"
on every prompt is a no-op.

## TL;DR

```bash
bot-cmder configure              # menu — pick a platform
bot-cmder configure telegram     # jump straight to Telegram
bot-cmder configure all          # walk all three in sequence
bot-cmder configure slack --dry-run    # preview the diff, don't write
```

Default `.env` location follows the same XDG search as `bot-cmder
init` (`$XDG_CONFIG_HOME/bot-cmder/.env`, typically
`~/.config/bot-cmder/.env`). Override with `--config-dir <path>`.

## What it asks per platform

### Telegram

| Step | Prompt | Notes |
|---|---|---|
| 1 | Ingestion mode (`webhook` / `polling`) | `polling` needs no public URL — good for home labs |
| 2 | `TELEGRAM_TOKEN` (secret) | Format `<digits>:<35+ chars>` from [@BotFather](https://t.me/BotFather) |
| 3 (webhook only) | `TELEGRAM_WEBHOOK_SECRET` (secret) | Offers to auto-generate via `secrets.token_urlsafe(32)` |

### Discord

| Step | Prompt | Notes |
|---|---|---|
| 1 | Ingestion mode (`interactions` / `gateway`) | `gateway` needs no URL but **loses slash commands** |
| 2 | `DISCORD_BOT_TOKEN` (secret) | Discord dev portal → Bot → Reset Token |
| 3 | `DISCORD_APPLICATION_ID` | Numeric snowflake (17-20 digits) from app URL |
| 4 (interactions only) | `DISCORD_PUBLIC_KEY` | 64 hex chars from General Information page |
| 5 (optional) | `DISCORD_GUILD_ID` | Read ONLY by `bot-cmder discord-register`, not by the bot. Blank to skip |

### Slack

| Step | Prompt | Notes |
|---|---|---|
| 1 | Ingestion mode (`events` / `socket`) | `socket` needs no URL |
| 2 | `SLACK_BOT_TOKEN` (secret) | Starts with `xoxb-`. OAuth & Permissions page |
| 3 (events only) | `SLACK_SIGNING_SECRET` (secret) | 32 hex chars. Basic Information → App Credentials |
| 3 (socket only) | `SLACK_APP_TOKEN` (secret) | Starts with `xapp-`. Basic Information → App-Level Tokens |
| 4 (events only, optional) | `SLACK_REQUEST_URL` | Read ONLY by `bot-cmder slack-manifest`. Falls back to `NGROK_DOMAIN` if blank |
| 4 (socket only, optional) | `SLACK_SIGNING_SECRET` | Slack still surfaces it for legacy events APIs; bot doesn't use it in pure socket mode |

## Existing-value UX

If a key already has a value, the wizard shows:

```
? TELEGRAM_TOKEN = 123···xYz3
  ❯ Keep current
    Replace
    Clear (empty value)
```

`Keep` is the default (one Enter). `Replace` re-prompts. `Clear`
writes `KEY=` (empty value — the placeholder comment above it stays
intact for a future re-edit). The masked preview shows enough to
confirm WHICH token is there without exposing it in scrollback.

## Live API validation

After credentials are collected (before writing), the wizard asks:

```
? Validate Telegram credentials against the API now? (y/N)
```

Default `No` — one Enter to skip. Pick `Yes` and the wizard calls:

- **Telegram**: `GET https://api.telegram.org/bot<TOKEN>/getMe`
- **Discord**: `GET https://discord.com/api/v10/users/@me`
- **Slack**: `POST https://slack.com/api/auth.test`

Each with a 5-second timeout. Success surfaces the bot's name /
workspace name as confirmation.

If validation fails:

```
✗ Telegram says: Unauthorized
? Validation failed. What now?
  ❯ Retry (e.g. fixed token in another window)
    Save anyway (I know what I'm doing)
    Abort (discard changes)
```

- **Retry** — re-runs the validator (useful if you fix the token in
  another window)
- **Save anyway** — operator override: write the .env despite
  validation failure (network glitch, intentional invalid token for
  staging, etc.)
- **Abort** — discards ALL changes for this platform (the .env is
  not touched). Other platforms' changes in `bot-cmder configure all`
  mode are NOT rolled back, since they're independent

## `--dry-run`

Prints the unified-diff of what would be written, then exits 0
without touching .env:

```diff
--- /home/zonda/.config/bot-cmder/.env (current)
+++ /home/zonda/.config/bot-cmder/.env (proposed)
@@ -3,3 +3,7 @@
 BOT_CMDER_MASTER_KEY=...

 # --- added by `bot-cmder configure` ---
+TELEGRAM_MODE=polling
+TELEGRAM_TOKEN=123:fake-token-...
```

Useful for code-review-style auditing of what the wizard will do
before committing.

## `--non-interactive`

Exit 1 with a clear "would have prompted: …" message instead of
asking. For CI smoke tests that want to assert the wizard *would*
ask for something specific.

Without `--non-interactive`, if there's no TTY, questionary's own
error surfaces — explicit `--non-interactive` is the documented way
to indicate "this is a non-interactive context".

## Surgical-update guarantees

Critical contracts pinned by tests:

- `BOT_CMDER_MASTER_KEY` line never touched by the wizard
- Existing values for platforms NOT being configured this run survive
  byte-for-byte (configure telegram doesn't touch Discord or Slack
  values)
- File mode forced to `0o600` on every save (matches `init`'s chmod)
- Atomic write via tempfile + `os.replace` — a Ctrl-C mid-save can't
  leave a half-written .env
- Comments + blank lines + section structure preserved across a
  round-trip

## What happens on Ctrl-C

`Aborted, no changes written.` to stderr + exit 130. The .env is
untouched (we never call `env.save()` if the flow returned via
KeyboardInterrupt).

## What it doesn't do (out of scope)

- `app.yaml` editing — `slack.reply_visibility` and friends still
  need manual edit. Tracked as a possible follow-up issue.
- OAuth / browser-redirect flows — the platform-side dance to GET
  the token (BotFather, Discord dev portal, Slack app manifest)
  still happens outside the wizard. See [`docs/discord-setup.md`](discord-setup.md),
  [`docs/slack-setup.md`](slack-setup.md), [`docs/telegram-polling.md`](telegram-polling.md).
- Auto-running `bot-cmder discord-register` / `bot-cmder slack-manifest`
  after credentials written. Prints a hint, doesn't run — those are
  destructive (PUT-replaces the slash command list).
- Multi-account / multi-workspace per platform. The `.env` schema
  supports one of each.

## See also

- Operator setup walkthroughs: [`docs/discord-setup.md`](discord-setup.md),
  [`docs/slack-setup.md`](slack-setup.md), [`docs/telegram-polling.md`](telegram-polling.md)
- No-domain mode walkthroughs: [`docs/discord-gateway.md`](discord-gateway.md),
  [`docs/slack-socket-mode.md`](slack-socket-mode.md), [`docs/telegram-polling.md`](telegram-polling.md)
- Issue: [#45](https://github.com/zondatw/bot-cmder/issues/45)
