import logging

import uvicorn
from fastapi import FastAPI
from fastapi.logger import logger as fastapi_logger

from modules.hook import router as hook_router

logging.config.fileConfig("config/logging.conf", disable_existing_loggers=False)

app = FastAPI()


@app.get("/")
def read_root():
    return {"Hello": "World"}


app.include_router(hook_router)

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="localhost",
        port=8000,
    )
