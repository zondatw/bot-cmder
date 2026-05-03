from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bot_cmder.adapters.discord import DiscordAdapter, DiscordClient
from bot_cmder.adapters.discord import make_router as discord_router
from bot_cmder.adapters.slack import SlackAdapter, SlackClient
from bot_cmder.adapters.slack import make_router as slack_router
from bot_cmder.adapters.telegram import TelegramAdapter, TelegramClient
from bot_cmder.adapters.telegram import make_router as telegram_router
from bot_cmder.adapters.telegram.daemon import TelegramDaemon
from bot_cmder.audit.log import AuditLogger
from bot_cmder.auth.acl import check_allowed
from bot_cmder.auth.pending import PendingOTPSessions
from bot_cmder.auth.secret_store import SecretStore
from bot_cmder.auth.totp import TOTPVerifier
from bot_cmder.commands.builtin import install_all
from bot_cmder.config.schema import AppConfig
from bot_cmder.config.settings import Settings, get_settings, load_app_config
from bot_cmder.connectors.ssh import SshConnectorPool
from bot_cmder.core.dispatcher import Dispatcher
from bot_cmder.core.registry import CommandRegistry

logger = logging.getLogger("bot_cmder")


def _setup_logging() -> None:
    """Configure the `bot_cmder.*` logger tree once.

    Idempotent so tests / repeated create_app() calls don't pile up
    handlers. We deliberately don't touch the root logger so uvicorn's
    own access logs keep their formatting.
    """
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.propagate = False


def _build_totp(settings: Settings, config: AppConfig) -> tuple[TOTPVerifier | None, PendingOTPSessions | None]:
    """Initialize TOTP infrastructure if BOT_CMDER_MASTER_KEY is set.

    When the master key is missing, the OTP gate stays disabled —
    privileged commands will then have no way to authorize and the
    Dispatcher's bypass-when-pending-is-None branch refuses to run
    them at all. Logged loudly because most prod deploys want it on.
    """
    if not settings.bot_cmder_master_key:
        logger.warning("BOT_CMDER_MASTER_KEY not set — TOTP disabled, privileged commands cannot be authorized")
        return None, None
    try:
        store = SecretStore(config.totp.secret_store_path, settings.bot_cmder_master_key)
    except ValueError:
        logger.exception("BOT_CMDER_MASTER_KEY is invalid; TOTP disabled")
        return None, None
    totp = TOTPVerifier(store)
    pending = PendingOTPSessions(ttl_s=config.totp.session_ttl_s)
    logger.info(
        "TOTP enabled: secret_store=%s, session_ttl=%ds",
        store.path,
        config.totp.session_ttl_s,
    )
    return totp, pending


def create_app() -> FastAPI:
    _setup_logging()
    settings = get_settings()
    config = load_app_config(settings)

    registry = CommandRegistry()
    audit = AuditLogger(config.audit.path)

    totp, pending = _build_totp(settings, config)
    ssh_pool = SshConnectorPool(config.hosts, config.ssh)
    if config.hosts:
        logger.info(
            "ssh pool configured: %d host(s) — %s",
            len(config.hosts),
            ", ".join(sorted(config.hosts)),
        )
    install_all(registry, pending=pending, totp=totp, audit=audit, ssh_pool=ssh_pool, config=config)

    dispatcher = Dispatcher(
        registry=registry,
        config=config,
        audit=audit,
        acl_check=check_allowed,
        pending=pending,
    )

    # Telegram has two ingestion modes (Phase 6a). Validate the env
    # var here so an obvious typo (TELEGRAM_MODE=poling) fails fast
    # at startup with a clear message instead of silently picking the
    # default.
    if settings.telegram_mode not in {"webhook", "polling"}:
        raise ValueError(f"TELEGRAM_MODE must be 'webhook' or 'polling', got {settings.telegram_mode!r}")

    telegram_client: TelegramClient | None = None
    telegram_adapter: TelegramAdapter | None = None
    telegram_router_obj = None
    telegram_daemon: TelegramDaemon | None = None
    if settings.telegram_token:
        telegram_client = TelegramClient(settings.telegram_token)
        telegram_adapter = TelegramAdapter(telegram_client)
        if settings.telegram_mode == "polling":
            telegram_daemon = TelegramDaemon(
                telegram_client,
                telegram_adapter,
                dispatcher,
                long_poll_timeout_s=settings.telegram_polling_timeout_s,
                drop_pending_on_start=settings.telegram_polling_drop_pending,
            )
        else:
            telegram_router_obj = telegram_router(
                telegram_adapter,
                dispatcher,
                webhook_secret=settings.telegram_webhook_secret,
            )

    discord_client: DiscordClient | None = None
    discord_router_obj = None
    if settings.discord_public_key and settings.discord_bot_token and settings.discord_application_id:
        discord_client = DiscordClient(
            bot_token=settings.discord_bot_token,
            application_id=settings.discord_application_id,
        )
        discord_adapter = DiscordAdapter(discord_client)
        try:
            discord_router_obj = discord_router(
                discord_adapter,
                dispatcher,
                public_key_hex=settings.discord_public_key,
            )
        except ValueError:
            logger.exception("DISCORD_PUBLIC_KEY invalid; discord adapter disabled")
            discord_router_obj = None

    slack_client: SlackClient | None = None
    slack_router_obj = None
    if settings.slack_signing_secret:
        # SLACK_BOT_TOKEN is currently unused for replies (response_url
        # is self-authorizing) but we still accept None — Phase 6 socket
        # mode + future chat.postMessage paths will flip the contract.
        slack_client = SlackClient(bot_token=settings.slack_bot_token)
        slack_adapter = SlackAdapter(slack_client, config.slack)
        slack_router_obj = slack_router(
            slack_adapter,
            dispatcher,
            signing_secret=settings.slack_signing_secret,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        names = ", ".join(c.name for c in registry.all())
        logger.info("commands registered: %s", names)
        daemon_task: asyncio.Task | None = None
        if telegram_client is not None:
            logger.info("telegram adapter mounted (mode=%s)", settings.telegram_mode)
            await _sync_telegram_command_menu(telegram_client, registry)
            if telegram_daemon is not None:
                daemon_task = asyncio.create_task(
                    telegram_daemon.run(),
                    name="telegram-daemon",
                )
        else:
            logger.warning("TELEGRAM_TOKEN not set; telegram adapter disabled")
        if discord_router_obj is not None:
            logger.info("discord adapter mounted (slash commands push via scripts/register_discord_commands.py)")
        else:
            logger.warning(
                "discord adapter disabled (need DISCORD_PUBLIC_KEY + DISCORD_BOT_TOKEN + DISCORD_APPLICATION_ID)"
            )
        if slack_router_obj is not None:
            logger.info(
                "slack adapter mounted (reply_visibility=%s, %d override(s))",
                config.slack.reply_visibility,
                len(config.slack.visibility_overrides),
            )
        else:
            logger.warning("slack adapter disabled (need SLACK_SIGNING_SECRET)")
        try:
            yield
        finally:
            # Stop the polling daemon FIRST, before closing the client
            # it depends on, otherwise the in-flight long-poll request
            # would error on the closed transport.
            if daemon_task is not None:
                daemon_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await daemon_task
            if telegram_client is not None:
                await telegram_client.aclose()
            if discord_client is not None:
                await discord_client.aclose()
            if slack_client is not None:
                await slack_client.aclose()
            await ssh_pool.close_all()

    app = FastAPI(lifespan=lifespan, title="bot-cmder", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    if telegram_router_obj is not None:
        app.include_router(telegram_router_obj)
    if discord_router_obj is not None:
        app.include_router(discord_router_obj)
    if slack_router_obj is not None:
        app.include_router(slack_router_obj)

    return app


_TELEGRAM_VALID_COMMAND_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


async def _sync_telegram_command_menu(client: TelegramClient, registry: CommandRegistry) -> None:
    """Push the registry into Telegram's slash-command autocomplete menu.

    Telegram requires command names to match `^[a-z][a-z0-9_]{0,31}$`.
    A single hyphenated name (`service-restart`) makes setMyCommands
    reject the entire batch with HTTP 400 — there is no per-entry
    partial accept. We filter locally so one bad name can't take the
    whole menu down. Skipped names still work in chat (the dispatcher
    doesn't apply this regex), they just don't autocomplete.

    Best-effort: a network/auth failure here is logged but never blocks
    the app from accepting webhooks.
    """
    # Push one menu entry per top-level surface: each registered Command
    # AND each Router. Subcommands (`service restart`) intentionally
    # don't show up — they're discovered via `/<router> help` instead,
    # which keeps the menu lean as the registry grows.
    accepted: list[dict[str, str]] = []
    skipped: list[str] = []
    for c in registry.all():
        if _TELEGRAM_VALID_COMMAND_NAME.match(c.name):
            accepted.append({"command": c.name, "description": (c.description or c.name)[:256]})
        else:
            skipped.append(c.name)
    for r in registry.all_routers():
        if _TELEGRAM_VALID_COMMAND_NAME.match(r.name):
            sub_count = len(r.subcommand_names())
            desc = f"{r.description} ({sub_count} subcommands — try /{r.name} help)"
            accepted.append({"command": r.name, "description": desc[:256]})
        else:
            skipped.append(r.name)
    if skipped:
        logger.warning(
            "skipping %d command(s) from telegram menu (name violates ^[a-z][a-z0-9_]{0,31}$): %s",
            len(skipped),
            ", ".join(skipped),
        )
    try:
        await client.set_my_commands(accepted)
        logger.info("telegram command menu synced (%d entries)", len(accepted))
    except Exception:
        logger.warning("failed to sync telegram command menu", exc_info=True)


app = create_app()
