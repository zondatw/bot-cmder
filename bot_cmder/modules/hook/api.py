from fastapi import APIRouter
from fastapi.logger import logger as fastapi_logger

from .serializer import HookRequest, HookResponse

router = APIRouter()


@router.post("/bot/hook")
def bot_hook(req: HookRequest) -> HookResponse:
    fastapi_logger.debug(req)
    fastapi_logger.debug("-debug-")
    fastapi_logger.info("-info-")
    fastapi_logger.warning("-warning-")
    fastapi_logger.error("-error-")
    return {"ok": True, "result": True, "description": "Get hook"}
