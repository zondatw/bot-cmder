from fastapi import APIRouter
from fastapi.logger import logger as fastapi_logger

from .serializer import HookRequest, HookResponse
from .model import BotCommand
from .constants import TelegramHook

router = APIRouter()

bot_command = BotCommand()


@router.post("/bot/hook")
async def bot_hook(req: HookRequest) -> HookResponse:
    fastapi_logger.debug(req)

    if req.message:
        if (
            req.message.entities
            and req.message.entities[0].type == TelegramHook.Entities.BOT_COMMAND.value
        ):
            res, message = bot_command.request(
                req.message.chat.id, req.message.message_id, req.message.text
            )
    elif req.callback_query:
        fastapi_logger.debug(f"Callback query: {req.callback_query.data}")
        res, message = bot_command.request(
            req.callback_query.from_.id,
            req.callback_query.message.message_id,
            req.callback_query.data,
        )

    return {"ok": True, "result": True, "description": "Get hook"}
