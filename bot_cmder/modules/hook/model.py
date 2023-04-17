from fastapi.logger import logger as fastapi_logger


class BotCommand:
    def __init__(self):
        pass

    def request(self, command: str):
        self.parse(command)

    def parse(self, command: str):
        fastapi_logger.debug(f"Get command: {command}")

    def response(self):
        pass
