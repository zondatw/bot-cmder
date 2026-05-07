# Audit log rotation

`bot_cmder.audit.log.AuditLogger` writes append-only JSONL to a single
active file. Without rotation that file grows forever — eventually
fills disk + makes forensic queries painful (`jq` over multi-GB
files isn't fun).

Issue #28 added built-in rotation. Two-axis trigger, gzip-compressed
rotated files, retention by count. Defaults are tuned for a typical
SRE deployment; tune in `app.yaml` under `audit.rotation` if your
volume is unusual.

## Defaults

```yaml
audit:
  path: /var/lib/bot-cmder/audit.jsonl   # or whatever your state dir resolves to
  rotation:
    max_bytes: 100000000   # 100 MB hard cap on active file
    when: midnight         # rotate at UTC 00:00 every day
    backup_count: 7        # keep one week of rotated files
    compress: true         # gzip rotated files
```

Steady-state disk budget under defaults:

| File | Size |
|---|---|
| Active `audit.jsonl` | ≤ 100 MB |
| 7 × rotated `audit.jsonl.<ts>.gz` | ~10 MB each compressed → 70 MB total |
| **Total** | **≤ 170 MB** |

## How rotation fires

On every `log()` call, inside the same lock that serializes writes,
`AuditLogger._maybe_rotate()` checks two triggers:

1. **Time** — has wall-clock UTC passed the next `when` boundary
   computed at startup? (e.g. for `midnight`, the next
   00:00 UTC anchor.)
2. **Size** — would the in-flight line push the active file past
   `max_bytes`?

Either firing causes rotation: rename `audit.jsonl` →
`audit.jsonl.<UTC-timestamp>` (e.g. `audit.jsonl.2026-05-07T00-00-00Z`),
gzip if `compress: true`, prune oldest past `backup_count`. The
in-flight line then lands in a freshly-created `audit.jsonl`.

Rotation is **not skipped if the file is empty** — but the empty-file
fast path skips the rename + gzip, just advances the time-rotation
boundary and continues. So an idle bot doesn't churn through empty
hourly files.

## Disabling rotation

If you have an external rotator (logrotate, vector, fluent-bit) and
want it to handle everything:

```yaml
audit:
  rotation:
    max_bytes: 0
    when: off
```

The bot logs a startup warning so you don't accidentally end up with
unbounded growth. Don't pair built-in + external — they'll race on
the rename and confuse each other.

## Inspecting rotated files

Plain (uncompressed):

```bash
jq 'select(.event == "EXECUTED")' /var/lib/bot-cmder/audit.jsonl.2026-05-07T00-00-00Z
```

Gzipped (default):

```bash
zcat /var/lib/bot-cmder/audit.jsonl.2026-05-07T00-00-00Z.gz | jq 'select(.event == "EXECUTED")'
# or
gunzip -c /var/lib/bot-cmder/audit.jsonl.2026-05-07T00-00-00Z.gz | jq ...
```

Cross-day forensic search ("when did SSH to host-X happen this week?"):

```bash
zgrep -h '"host":"host-X"' /var/lib/bot-cmder/audit.jsonl.* | \
  jq -r 'select(.event == "EXECUTED") | "\(.ts) \(.user) \(.command)"'
```

`zgrep` handles both gzipped and plain files transparently — won't
choke on a mixed retention window during a `compress` flag flip.

## Choosing a `when` value

| Value | Use case |
|---|---|
| `off` | External rotator handles everything; size cap still applies |
| `hourly` | Very high-volume deployments where hourly files keep `jq` queries snappy |
| `daily` | Daily 24h-since-first-write rotation |
| `midnight` | **Default.** Files match calendar days exactly; matches "what happened on date X" queries |
| `weekly` | Quiet bots; one rotated file per Monday |

## Choosing `max_bytes`

Defaults to 100 MB. Rough sizing rule: pick the size that matches how
much data your operators want to keep in front of them at one time
during a live incident:

- Small home lab / 10 events/day → drop to **10 MB** (still rotates
  monthly even if `when=off`)
- Typical SRE team / 100s of events/day → **100 MB** is fine
- High-volume integration platform / 1000s/min → bump to **500 MB +
  hourly rotation** so individual files are still grep-able

`0` disables size-based rotation. Only meaningful paired with
`when != off` (otherwise nothing rotates).

## Choosing `backup_count`

Default 7 = one week at daily rotation. Pick by retention policy
crossed with rotation cadence:

| Cadence × backups | Retention window |
|---|---|
| `midnight` × 7 | 1 week |
| `midnight` × 30 | 1 month |
| `midnight` × 365 | 1 year (probably overkill — use external archive) |
| `hourly` × 24 | 1 day (rolling) |
| `weekly` × 4 | 1 month |

Compliance regimes often dictate this (e.g. 90 days for SOC 2).
Implement that by setting `backup_count: 90` with `when: midnight`,
and confirm the disk budget per the table above.

## Concurrency

Rotation runs inside the same `threading.Lock` that serializes
writes. Concurrent log calls cannot interleave bytes within a JSONL
line, AND cannot write to a file mid-rename. The cost: rotation
adds O(few ms) of latency to the triggering write (rename + gzip
of a 100 MB file is fast on SSDs, a few hundred ms on HDDs at the
high end).

If rotation latency ever shows up in profiling — switch the
gzip step to a fire-and-forget background task. Today's
synchronous design favors simplicity: rotation is rare (hourly at
worst), and a brief stall on a single audit write is acceptable.

## Failure modes

| Failure | Result |
|---|---|
| Disk full during rotation rename | `OSError` caught, logged via `bot_cmder.audit.log` to the bot's log channel; the bot keeps appending to the (now over-cap) active file — data preserved, operator gets a visible warning |
| Compression fails mid-gzip | Partial `.gz` unlinked; the plain rotated file is preserved (data not lost); operator sees warning |
| Rename collision on the second-precision suffix | Counter appended (`audit.jsonl.<ts>.1`, `.2`, ...) — never overwrites prior data |
| Clock skew | `<ts>` in the filename reflects rotation moment; the `ts` field on each individual JSONL line remains the authoritative event time |
| Crash mid-rotation | Either the rename completed (rotated file in place) or didn't (active file intact). Atomic rename guarantees no half-state |

## Tuning per platform

| Platform | Recommended override |
|---|---|
| Local dev with `./var/audit.jsonl` | `max_bytes: 10000000` (10 MB), `backup_count: 3` — lighter retention, easier `jq` |
| Docker container with named volume | Defaults |
| k8s with `PersistentVolumeClaim` | Defaults; rotation hits the PVC's underlying filesystem like any other write — no extra config needed |
| Multi-instance HA | **Not supported** — state dir is single-writer (issue #20 documents this). Built-in rotation is single-writer too; running multiple bot instances against one audit file is undefined behavior |
