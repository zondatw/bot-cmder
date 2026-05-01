from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bot_cmder.adapters.telegram import TelegramAdapter, TelegramClient
from bot_cmder.adapters.telegram import make_router as telegram_router
from bot_cmder.audit.log import AuditLogger
from bot_cmder.auth.acl import check_allowed
from bot_cmder.commands.builtin import install_all
from bot_cmder.config.settings import get_settings, load_app_config
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


def create_app() -> FastAPI:
    _setup_logging()
    settings = get_settings()
    config = load_app_config(settings)

    registry = CommandRegistry()
    install_all(registry)

    audit = AuditLogger(config.audit.path)

    dispatcher = Dispatcher(
        registry=registry,
        config=config,
        audit=audit,
        acl_check=check_allowed,
    )

    telegram_client: TelegramClient | None = None
    telegram_router_obj = None
    if settings.telegram_token:
        telegram_client = TelegramClient(settings.telegram_token)
        telegram_adapter = TelegramAdapter(telegram_client)
        telegram_router_obj = telegram_router(
            telegram_adapter,
            dispatcher,
            webhook_secret=settings.telegram_webhook_secret,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        names = ", ".join(c.name for c in registry.all())
        logger.info("commands registered: %s", names)
        if telegram_client is not None:
            logger.info("telegram adapter mounted")
        else:
            logger.warning("TELEGRAM_TOKEN not set; telegram adapter disabled")
        try:
            yield
        finally:
            if telegram_client is not None:
                await telegram_client.aclose()

    app = FastAPI(lifespan=lifespan, title="bot-cmder", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    if telegram_router_obj is not None:
        app.include_router(telegram_router_obj)

    return app


app = create_app()
