import uvicorn

from bot_cmder.main import app
from bot_cmder.settings import TELEGRAM_TOKEN

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="localhost",
        port=8000,
    )
