import logging

import uvicorn
from fastapi import FastAPI
from fastapi.logger import logger as fastapi_logger
from pydantic import BaseModel

logging.config.fileConfig("config/logging.conf", disable_existing_loggers=False)

app = FastAPI()


class HookResponse(BaseModel):
    ok: bool
    result: bool
    description: str


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.post("/bot/hook")
def bot_hook(req: dict) -> HookResponse:
    fastapi_logger.debug(req)
    fastapi_logger.debug("-debug-")
    fastapi_logger.info("-info-")
    fastapi_logger.warning("-warning-")
    fastapi_logger.error("-error-")
    return {"ok": True, "result": True, "description": "Get hook"}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="localhost",
        port=8000,
    )
