import json

import requests
from fastapi.logger import logger as fastapi_logger

from bot_cmder.settings import TELEGRAM_TOKEN


class BotCommand:
    def __init__(self):
        pass

    def request(self, chat_id: int, command: str):
        self.parse(command)
        return send_inline_keyboard(TELEGRAM_TOKEN, chat_id, "yooooooo")

    def parse(self, command: str):
        fastapi_logger.debug(f"Get command: {command}")

    def response(self):
        pass


def send_inline_keyboard(token: str, chat_id: int, message: str):
    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": "apple",
                    "callback_data": "apple cmd",
                },
                {
                    "text": "orange",
                    "callback_data": "orange cmd",
                },
            ],
        ]
    }
    reply_markup = json.dumps(reply_markup)
    url = f"https://api.telegram.org/bot{token}/sendMessage?chat_id={chat_id}&text={message}&reply_markup={reply_markup}"
    response = requests.get(url)
    return response.status_code == 200, response.text
