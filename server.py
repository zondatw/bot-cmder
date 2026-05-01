import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "bot_cmder.main:app",
        host=os.getenv("BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("BIND_PORT", "47823")),
        reload=os.getenv("RELOAD", "0") == "1",
    )
