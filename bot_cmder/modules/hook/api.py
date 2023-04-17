from fastapi import APIRouter
from fastapi.logger import logger as fastapi_logger

from .serializer import HookRequest, HookResponse
from .model import BotCommand
from .constants import TelegramHook

router = APIRouter()

bot_command = BotCommand()


@router.post("/bot/hook")
async def bot_hook(req: HookRequest) -> HookResponse:
    fastapi_logger.debug(req.message)

    if (
        req.message.entities
        and req.message.entities[0].type == TelegramHook.Entities.BOT_COMMAND.value
    ):
        bot_command.request(req.message.text)
    return {"ok": True, "result": True, "description": "Get hook"}
