# OTP gate + emergency-bypass window

How `bot_cmder` enforces the second factor on PRIVILEGED commands,
and how SRE on-call can opt into a time-bounded bypass during an
incident (issue #15).

---

## Normal flow (Phase 2 contract)

Every PRIVILEGED command (`/kubectl`, `/ssh`, `/service restart`,
`/runbook run`, …) goes through the OTP gate before executing:

```
user → @sre_bot service restart hello --host gce
bot  → "Privileged command. Reply with: /otp <6-digit-code> within 120s"
user → /otp 123456
bot  → <SSH output>
```

Audit trail (every step is its own JSONL line):

```
OTP_REQUESTED   command=service_restart  args=[hello, --host, gce]  ttl_s=120
EXECUTED        command=service_restart  args=[hello, --host, gce]  via_otp=true
SSH_EXECUTED    host=gce  exit_code=0  duration_ms=...
```

Failure modes get distinct events so post-mortems can tell them
apart: `OTP_NO_PENDING` (typed `/otp` without prior privileged
command), `OTP_CROSS_CHAT` (submitted in a different channel than
the original), `OTP_EXPIRED` (took longer than 120s), `OTP_INVALID`
(wrong code).

---

## Emergency-bypass window (issue #15)

### Why

Mid-incident, every PRIVILEGED command costs the operator 5+
seconds of OTP friction (pull phone → read code → submit →
resume). Across 10 commands that's a minute on top of the
incident itself. The emergency window opens a time-bounded
exception: prove identity ONCE, get N minutes of bypass.

### Activation

Two-step (the bypass itself is a privileged action, so it gates
on its own OTP):

```
user → @sre_bot otp emergency 15
bot  → "Emergency activation requested (15 min). Reply with:
        /otp <6-digit-code> within 120s.
        Note: hard cap is enforced server-side; actual granted
        duration may be shorter."
user → /otp 123456
bot  → "🚨 EMERGENCY MODE ACTIVE for 15 min.
        All PRIVILEGED commands will run without OTP for
        telegram:1234567890 until 2026-01-01T12:15:00+00:00.
        Type `/otp end` to revoke early."
```

### During the window

PRIVILEGED commands run inline — no OTP prompt:

```
user → @sre_bot service restart hello --host gce
bot  → <SSH output>          ← no prompt, immediate
```

Audit gets a marker on every bypass:

```
EMERGENCY_OTP_BYPASS  command=service_restart  window_remaining_s=820
EXECUTED              command=service_restart  via_emergency_otp=true
SSH_EXECUTED          host=gce  exit_code=0
```

To find every command that ran during a bypass window, filter on
the marker:

```shell
jq 'select(.via_emergency_otp == true)' var/audit.jsonl
```

### Sub-commands

| Command | Behavior | Needs OTP? |
|---|---|---|
| `/otp <code>` | Submit OTP for a pending privileged command, OR for a pending emergency-activation | — (this IS the OTP) |
| `/otp emergency <minutes>` | Stash an activation request; opens window once OTP submitted | **Yes** (via the standard pending-session flow) |
| `/otp end` | Revoke any active emergency window for the caller | **No** — closing your own gate doesn't need re-auth |
| `/otp status` | Show "off" or "ON, Xs remaining (expires at ...)" | **No** |

### Hard cap

`config/app.yaml` controls the operator-tunable maximum:

```yaml
totp:
  emergency_max_minutes: 60   # default
```

If `/otp emergency 480` is requested, the window is opened for 60
min (not 480). The reply text + `EMERGENCY_OTP_GRANTED` audit both
surface the cap so the operator notices:

```
🚨 EMERGENCY MODE ACTIVE for 60 min (requested 480, capped to 60 by emergency_max_minutes).
```

### Auto-revoke triggers

| Trigger | Audit event |
|---|---|
| Time expired (lazy cleanup, no background sweeper) | `EMERGENCY_OTP_EXPIRED`* |
| Operator typed `/otp end` | `EMERGENCY_OTP_REVOKED` |
| Bot process restart | (no event — in-memory state simply gone, fail-safe) |

\* `EMERGENCY_OTP_EXPIRED` is emitted lazily — when the dispatcher
or `/otp status` checks an existing window past its `expires_at`,
the window is silently dropped and the next access logs nothing
beyond a normal `OTP_REQUESTED`. (No background sweeper task to
keep the architecture simple; in practice every PRIVILEGED command
attempt re-checks the window so it gets cleaned up promptly.)

---

## Security notes

### Is this safe?

Less safe than always-OTP — by design. The window is a deliberate
trade-off between operator productivity and the cost of a bypassed
factor. Mitigations:

- **Activation requires OTP.** No self-bypass: opening the window
  is itself privileged.
- **Time-bounded.** Hard cap (default 60 min, tunable down). A
  stolen chat session during a window is bounded in damage by
  the cap.
- **Per `norm_id`, not bot-wide.** Other users' commands still
  hit the OTP gate — bypass is scoped to the user who activated.
- **No auto-renewal.** Window doesn't slide forward on use; you
  re-activate by re-submitting OTP.
- **Audit trail.** Every bypass is logged with
  `via_emergency_otp:true` AND a separate `EMERGENCY_OTP_BYPASS`
  event with the remaining-window seconds. Easy to filter
  post-incident: "show me everything that ran without OTP, and
  was the window plausibly active?".

### When to NOT use it

- **Routine ops.** If you're not actively in incident response,
  go through the OTP gate per command. The friction is the point.
- **Long-running incidents (>60 min).** Re-activate per cap rather
  than raising the cap. If you find yourself raising the cap, it's
  a signal to look at OTP usability (mobile auth-app proximity,
  alias resolution to canonical user — a future hardening item).
- **Shared bot tokens / shared chat sessions.** The bypass window
  is per-norm_id; if multiple humans share one chat identity, all
  of them get the bypass. Use distinct accounts.

### Multi-instance deployments

`EmergencyWindows` is in-memory per process. A 2-instance HA
deployment behind a load balancer means an active window on
instance A doesn't apply to instance B; the user might see OTP
prompts for some commands and bypasses for others depending on
which instance handled the request. **Not recommended for HA today.**
Future hardening could move state to a shared store (Redis /
SQLite) — out of scope for issue #15.

---

## Audit event reference

| Event | Emitted by | When |
|---|---|---|
| `OTP_REQUESTED` | dispatcher | PRIVILEGED command with no active window — normal stash |
| `OTP_REQUESTED` | `/otp` | `/otp emergency <minutes>` — stash an activation request (command field is `__emergency_activate__`) |
| `OTP_NO_PENDING` | `/otp` | `/otp <code>` with no stashed session |
| `OTP_CROSS_CHAT` | `/otp` | OTP submitted in different platform/chat than original stash |
| `OTP_EXPIRED` | `/otp` | Pending session aged past `session_ttl_s` (120s default) |
| `OTP_INVALID` | `/otp` | Wrong code |
| `OTP_COMMAND_GONE` | `/otp` | OTP valid but the resumed command is no longer registered |
| `EXECUTED` | dispatcher / `/otp` | Command handler ran. `via_otp:true` if resumed via `/otp`; `via_emergency_otp:true` if window-bypassed |
| `EMERGENCY_OTP_GRANTED` | `/otp` | Activation succeeded — window opened |
| `EMERGENCY_OTP_BYPASS` | dispatcher | Each PRIVILEGED command run during an active window |
| `EMERGENCY_OTP_REVOKED` | `/otp` | `/otp end` on an active window |
| `EMERGENCY_OTP_INVALID_DURATION` | `/otp` | `/otp emergency <bad-value>` (non-int or <1) |
