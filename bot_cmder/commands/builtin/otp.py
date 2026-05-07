"""`/otp` builtin — TOTP submission + emergency-window control.

Sub-syntaxes (issue #15 added the emergency/end/status trio; issue
#18 added the explicit `code` form for Discord parity):

    /otp <6-digit-code>           — submit OTP for a pending privileged
                                     command, OR resume an emergency-
                                     activation request stashed by
                                     `/otp emergency <minutes>`.
                                     Telegram-native form (free text).
    /otp code <6-digit-code>      — same as above. Discord-native form:
                                     once /otp uses sub-commands for
                                     emergency/end/status, the bare-
                                     code path also has to live under
                                     a sub-command (Discord rule: no
                                     mixing flat options with sub-
                                     commands at the same level).
    /otp emergency <minutes>      — start emergency-activation flow:
                                     stashes a session that opens an
                                     OTP-bypass window when satisfied
                                     by the next `/otp <code>`
    /otp end                      — revoke any active emergency window
                                     for the caller (no OTP required —
                                     closing your own gate doesn't
                                     need re-auth)
    /otp status                   — show current emergency-window
                                     state (off / Xs remaining)

Distinct audit events for each failure mode so post-mortems can tell
"no session" from "wrong code" from "leaked across chats", plus the
emergency-specific events from issue #15:

    EMERGENCY_OTP_GRANTED          — activation succeeded
    EMERGENCY_OTP_REVOKED          — /otp end called on active window
    EMERGENCY_OTP_INVALID_DURATION — /otp emergency <bad-value>

(Per-command bypass events `EMERGENCY_OTP_BYPASS` are emitted by the
dispatcher, not here, since they fire for every PRIVILEGED command
during the window — see `bot_cmder/core/dispatcher.py`.)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import OutgoingResponse
from bot_cmder.core.redact import redact_args_for_audit
from bot_cmder.core.registry import CommandRegistry, Risk, register

if TYPE_CHECKING:
    from bot_cmder.audit.log import AuditLogger
    from bot_cmder.auth.emergency import EmergencyWindows
    from bot_cmder.auth.lockout import OTPLockoutState
    from bot_cmder.auth.pending import PendingOTPSessions
    from bot_cmder.auth.totp import TOTPVerifier


# Reserved synthetic command_name stashed in PendingOTPSession for an
# emergency-activation request. When `/otp <code>` pops a session
# with this name, instead of resuming a real command we activate the
# bypass window. Underscores keep it from colliding with any real
# command name (which the registry validates as `[a-z][a-z0-9_]+`).
_EMERGENCY_ACTIVATE_TAG = "__emergency_activate__"


def install(
    registry: CommandRegistry,
    *,
    pending: PendingOTPSessions,
    totp: TOTPVerifier,
    audit: AuditLogger,
    emergency: EmergencyWindows | None = None,
    lockout: OTPLockoutState | None = None,
) -> None:
    """Register `/otp` with the registry.

    `emergency` is optional for backwards compatibility — Phase 1-7
    tests that build a registry without it (no emergency flow) still
    work; the new sub-commands return a usage hint instead of crashing.

    `lockout` (issue #33) is also optional for backwards compatibility.
    When wired, the handler pre-checks for an active lockout BEFORE
    popping the pending session — a locked-out user can't burn through
    pending sessions either. After OTP_INVALID, the failure is recorded
    and may trigger a fresh lockout. After successful verify, the
    failure counter resets.
    """

    @register(
        "otp",
        risk=Risk.SAFE,
        description="Submit OTP code, or control emergency-bypass window (subcommands: emergency / end / status)",
        registry=registry,
    )
    async def _otp(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
        if not args:
            return OutgoingResponse.text_reply(_USAGE)

        sub = args[0]

        # Issue #15 sub-commands. Order matters: `end` and `status`
        # don't go through the OTP-stash flow at all; `emergency`
        # creates a stash; bare digits fall through to the existing
        # OTP-code path (which now also handles emergency activation
        # as one of its branches).
        #
        # Each branch enforces its own arity so a typo like
        # `/otp end now` still routes to the usage hint instead of
        # silently ignoring the extra word.
        if sub == "end":
            if len(args) != 1:
                return OutgoingResponse.text_reply(_USAGE)
            return _handle_end(ctx, emergency=emergency, audit=audit)
        if sub == "status":
            if len(args) != 1:
                return OutgoingResponse.text_reply(_USAGE)
            return _handle_status(ctx, emergency=emergency)
        if sub == "emergency":
            return _handle_emergency_request(ctx, args[1:], pending=pending, audit=audit)
        if sub == "code":
            # Issue #18 — Discord-native invocation form `/otp code <code>`.
            # Discord's slash-command schema can't mix flat options with
            # sub-commands at the same level, so once /otp uses
            # sub-commands for emergency/end/status, the bare-code form
            # also has to live under a sub-command. Telegram users still
            # use `/otp <code>` (handled by the fall-through below).
            if len(args) != 2:
                return OutgoingResponse.text_reply(_USAGE)
            return await _handle_code(
                ctx,
                code=args[1],
                registry=registry,
                pending=pending,
                totp=totp,
                audit=audit,
                emergency=emergency,
                lockout=lockout,
            )

        # Default: treat sub as a 6-digit OTP code (existing Phase 2
        # contract — strict 1-arg). The handler is shared between
        # "complete a pending privileged command" and "complete a
        # pending emergency-activation" — indistinguishable from the
        # user's POV at this step.
        if len(args) != 1:
            return OutgoingResponse.text_reply(_USAGE)
        return await _handle_code(
            ctx,
            code=sub,
            registry=registry,
            pending=pending,
            totp=totp,
            audit=audit,
            emergency=emergency,
            lockout=lockout,
        )


_USAGE = (
    "usage:\n"
    "  /otp <6-digit-code>          — submit OTP for a pending privileged command\n"
    "  /otp code <6-digit-code>     — same, Discord-native form (see issue #18)\n"
    "  /otp emergency <minutes>     — open OTP-bypass window for incident response\n"
    "  /otp end                     — revoke any active emergency window\n"
    "  /otp status                  — show emergency window state"
)


def _handle_end(
    ctx: CommandContext,
    *,
    emergency: EmergencyWindows | None,
    audit: AuditLogger,
) -> OutgoingResponse:
    if emergency is None:
        return OutgoingResponse.text_reply("emergency mode unavailable (server has no EmergencyWindows wired)")
    revoked = emergency.revoke(ctx.user.norm_id)
    if revoked is None:
        return OutgoingResponse.text_reply("no emergency window active")
    audit.log(
        event="EMERGENCY_OTP_REVOKED",
        platform=ctx.platform.value,
        user=ctx.user.norm_id,
        chat=ctx.chat_id,
        granted_at=revoked.granted_at.isoformat(),
        granted_minutes=revoked.granted_minutes,
        remaining_s=revoked.remaining_s(datetime.now(timezone.utc)),
    )
    return OutgoingResponse.text_reply("🔒 emergency mode revoked. PRIVILEGED commands now require OTP again.")


def _handle_status(
    ctx: CommandContext,
    *,
    emergency: EmergencyWindows | None,
) -> OutgoingResponse:
    if emergency is None:
        return OutgoingResponse.text_reply("emergency mode: unavailable (server has no EmergencyWindows wired)")
    window = emergency.get(ctx.user.norm_id)
    if window is None:
        return OutgoingResponse.text_reply("emergency mode: off")
    remaining = window.remaining_s(datetime.now(timezone.utc))
    return OutgoingResponse.text_reply(
        f"emergency mode: ON, {remaining}s remaining (granted {window.granted_minutes} min, "
        f"expires at {window.expires_at.isoformat(timespec='seconds')})"
    )


def _handle_emergency_request(
    ctx: CommandContext,
    rest: list[str],
    *,
    pending: PendingOTPSessions,
    audit: AuditLogger,
) -> OutgoingResponse:
    """First half of activation: parse + validate duration, stash a
    pending session that the next `/otp <code>` will turn into an
    actual EmergencyWindow. Activation itself happens in `_handle_code`
    when it sees the `_EMERGENCY_ACTIVATE_TAG` session."""
    if len(rest) != 1:
        return OutgoingResponse.text_reply("usage: /otp emergency <minutes>")
    try:
        minutes = int(rest[0])
        if minutes < 1:
            raise ValueError
    except ValueError:
        audit.log(
            event="EMERGENCY_OTP_INVALID_DURATION",
            platform=ctx.platform.value,
            user=ctx.user.norm_id,
            chat=ctx.chat_id,
            requested=rest[0],
        )
        return OutgoingResponse.text_reply(f"emergency duration must be a positive integer (got {rest[0]!r})")

    pending.stash(
        user_norm_id=ctx.user.norm_id,
        chat_id=ctx.chat_id,
        platform=ctx.platform,
        command_name=_EMERGENCY_ACTIVATE_TAG,
        args=[str(minutes)],
    )
    audit.log(
        event="OTP_REQUESTED",
        platform=ctx.platform.value,
        user=ctx.user.norm_id,
        chat=ctx.chat_id,
        command=_EMERGENCY_ACTIVATE_TAG,
        args=[str(minutes)],
        ttl_s=pending.ttl_s,
    )
    return OutgoingResponse.text_reply(
        f"Emergency activation requested ({minutes} min). "
        f"Reply with: /otp <6-digit-code> within {pending.ttl_s}s.\n"
        f"Note: hard cap is enforced server-side; actual granted duration may be shorter."
    )


async def _handle_code(
    ctx: CommandContext,
    *,
    code: str,
    registry: CommandRegistry,
    pending: PendingOTPSessions,
    totp: TOTPVerifier,
    audit: AuditLogger,
    emergency: EmergencyWindows | None,
    lockout: OTPLockoutState | None = None,
) -> OutgoingResponse:
    """Submit OTP code. Branches on what the popped pending session
    represents — a real command resume, or an emergency-activation
    request stashed by `_handle_emergency_request`.

    Issue #33 — pre-flight lockout check runs BEFORE pop()ping the
    pending session, so a locked-out user can't burn through pending
    sessions while their lockout window is active. After OTP_INVALID,
    the failure is recorded; after successful verify, the failure
    counter resets.
    """
    # Issue #33 pre-flight — check lockout BEFORE popping the pending
    # session. snapshot() doesn't mutate, so we can detect both
    # "still locked" and "just expired" cases without touching state.
    if lockout is not None and lockout.enabled:
        snap = lockout.snapshot(ctx.user.norm_id)
        if snap.locked:
            audit.log(
                event="OTP_LOCKED_OUT",
                platform=ctx.platform.value,
                user=ctx.user.norm_id,
                chat=ctx.chat_id,
                remaining_s=snap.remaining_s(datetime.now(timezone.utc)),
                failure_count=snap.failure_count,
            )
            remaining_min = (snap.remaining_s(datetime.now(timezone.utc)) + 59) // 60
            return OutgoingResponse.text_reply(
                f"OTP locked out (~{remaining_min} min remaining) after repeated failures. "
                "Wait or contact admin (`bot-cmder unlock-totp`)."
            )
        if snap.locked_until is not None:
            # Had a lockout that just expired — log the transition,
            # clean up, then fall through to normal verification.
            audit.log(
                event="OTP_LOCKOUT_EXPIRED",
                platform=ctx.platform.value,
                user=ctx.user.norm_id,
                chat=ctx.chat_id,
                expired_at=snap.locked_until.isoformat(),
            )
            lockout.reset(ctx.user.norm_id)

    session = pending.pop(ctx.user.norm_id)
    if session is None:
        audit.log(
            event="OTP_NO_PENDING",
            platform=ctx.platform.value,
            user=ctx.user.norm_id,
            chat=ctx.chat_id,
        )
        return OutgoingResponse.text_reply("no pending privileged command (or it expired)")

    if session.chat_id != ctx.chat_id or session.platform != ctx.platform:
        audit.log(
            event="OTP_CROSS_CHAT",
            platform=ctx.platform.value,
            user=ctx.user.norm_id,
            chat=ctx.chat_id,
            command=session.command_name,
            pending_chat=session.chat_id,
            pending_platform=session.platform.value,
        )
        return OutgoingResponse.text_reply("OTP must be submitted in the same chat as the original command")

    if pending.is_expired(session):
        audit.log(
            event="OTP_EXPIRED",
            platform=ctx.platform.value,
            user=ctx.user.norm_id,
            chat=ctx.chat_id,
            command=session.command_name,
        )
        return OutgoingResponse.text_reply("session expired; rerun the original command")

    if not totp.verify(ctx.user.norm_id, code):
        audit.log(
            event="OTP_INVALID",
            platform=ctx.platform.value,
            user=ctx.user.norm_id,
            chat=ctx.chat_id,
            command=session.command_name,
        )
        # Issue #33 — record this failure, possibly trigger lockout.
        if lockout is not None and lockout.enabled:
            triggered = lockout.record_failure(ctx.user.norm_id)
            if triggered:
                snap = lockout.snapshot(ctx.user.norm_id)
                audit.log(
                    event="OTP_LOCKOUT_TRIGGERED",
                    platform=ctx.platform.value,
                    user=ctx.user.norm_id,
                    chat=ctx.chat_id,
                    failure_count=snap.failure_count,
                    remaining_s=snap.remaining_s(datetime.now(timezone.utc)),
                    locked_until=snap.locked_until.isoformat() if snap.locked_until else None,
                )
                return OutgoingResponse.text_reply(
                    f"invalid code; locked out for ~{(snap.remaining_s(datetime.now(timezone.utc)) + 59) // 60} min "
                    f"after {snap.failure_count} failures"
                )
        return OutgoingResponse.text_reply("invalid code")

    # ✓ OTP verified. Issue #33 — clear failure counter so a one-shot
    # typo doesn't accumulate forever.
    if lockout is not None and lockout.enabled:
        lockout.reset(ctx.user.norm_id)

    # Branch on session type.
    if session.command_name == _EMERGENCY_ACTIVATE_TAG:
        return _activate_emergency_window(ctx, session.args, emergency=emergency, audit=audit)

    # Otherwise: standard "resume the original command" flow.
    return await _resume_pending_command(
        ctx,
        session=session,
        registry=registry,
        audit=audit,
    )


def _activate_emergency_window(
    ctx: CommandContext,
    session_args: list[str],
    *,
    emergency: EmergencyWindows | None,
    audit: AuditLogger,
) -> OutgoingResponse:
    if emergency is None:
        # Defensive: should be impossible if /otp emergency was used
        # to stash this (the request handler also requires emergency
        # to be wired), but guard regardless so a config drift can't
        # crash this code path.
        return OutgoingResponse.text_reply("emergency mode unavailable (server has no EmergencyWindows wired)")
    requested = int(session_args[0])  # validated at request time
    window = emergency.grant(ctx.user.norm_id, requested)
    audit.log(
        event="EMERGENCY_OTP_GRANTED",
        platform=ctx.platform.value,
        user=ctx.user.norm_id,
        chat=ctx.chat_id,
        requested_minutes=requested,
        granted_minutes=window.granted_minutes,
        expires_at=window.expires_at.isoformat(),
    )
    capped_note = ""
    if window.granted_minutes < requested:
        capped_note = f" (requested {requested}, capped to {window.granted_minutes} by emergency_max_minutes)"
    return OutgoingResponse.text_reply(
        f"🚨 EMERGENCY MODE ACTIVE for {window.granted_minutes} min{capped_note}.\n"
        f"All PRIVILEGED commands will run without OTP for {ctx.user.norm_id} until "
        f"{window.expires_at.isoformat(timespec='seconds')}.\n"
        f"Type `/otp end` to revoke early."
    )


async def _resume_pending_command(
    ctx: CommandContext,
    *,
    session,  # PendingOTPSession; avoiding the import here keeps cycle risk low
    registry: CommandRegistry,
    audit: AuditLogger,
) -> OutgoingResponse:
    """The Phase 2-original flow: re-invoke a stashed PRIVILEGED
    command's handler with its stashed args."""
    cmd = registry.find_command(session.command_name)
    if cmd is None:
        audit.log(
            event="OTP_COMMAND_GONE",
            platform=ctx.platform.value,
            user=ctx.user.norm_id,
            chat=ctx.chat_id,
            command=session.command_name,
        )
        return OutgoingResponse.text_reply(f"command no longer registered: /{session.command_name}")

    try:
        response = await asyncio.wait_for(cmd.handler(ctx, session.args), timeout=cmd.timeout_s)
    except asyncio.TimeoutError:
        audit.log(
            event="COMMAND_TIMEOUT",
            platform=ctx.platform.value,
            user=ctx.user.norm_id,
            chat=ctx.chat_id,
            command=cmd.name,
            args=redact_args_for_audit(cmd.name, session.args),
            via_otp=True,
            timeout_s=cmd.timeout_s,
        )
        return OutgoingResponse.text_reply(f"timeout after {cmd.timeout_s}s")
    except Exception as exc:
        audit.log(
            event="HANDLER_ERROR",
            platform=ctx.platform.value,
            user=ctx.user.norm_id,
            chat=ctx.chat_id,
            command=cmd.name,
            args=redact_args_for_audit(cmd.name, session.args),
            via_otp=True,
            error=repr(exc),
        )
        return OutgoingResponse.text_reply(f"error: {exc}")

    audit.log(
        event="EXECUTED",
        platform=ctx.platform.value,
        user=ctx.user.norm_id,
        chat=ctx.chat_id,
        command=cmd.name,
        args=redact_args_for_audit(cmd.name, session.args),
        via_otp=True,
    )
    # Propagate the RESUMED command's risk, not /otp's own SAFE,
    # so the Slack adapter (which uses risk for reply visibility)
    # treats `/otp 123456 → service_restart` output as PRIVILEGED
    # — i.e. ephemeral by default, not broadcast to the channel.
    response.risk = cmd.risk.value
    response.command_name = cmd.name
    # Make Slack's command-echo show the resumed command (e.g.
    # `/ssh hello uptime`) instead of the literal `/otp <redacted>`
    # the user typed — otherwise the chat reads as
    #     > /otp <redacted>
    #     unknown host: hello (known: gce)
    # which is disorienting (where did "hello" come from?).
    # Reconstruct from the synthetic name + stashed args, the same
    # canonical form the dispatcher recorded in OTP_REQUESTED audit.
    response.displayed_command = f"/{cmd.name} {' '.join(session.args)}".rstrip() if session.args else f"/{cmd.name}"
    return response
