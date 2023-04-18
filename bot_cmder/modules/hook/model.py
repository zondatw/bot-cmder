import json

import requests
from fastapi.logger import logger as fastapi_logger

from bot_cmder.settings import TELEGRAM_TOKEN


class BotCommand:
    def __init__(self):
        pass

    def request(self, chat_id: int, message_id: int, command: str):
        self.parse(command)

        if command == "/cmd":
            return send_inline_keyboard(TELEGRAM_TOKEN, chat_id, "yooooooo")
        elif command == "apple cmd":
            return answer_callback_query_edit_message_reply_markup(
                TELEGRAM_TOKEN, chat_id, message_id, "stage 2"
            )
        elif command == "orange cmd":
            return answer_callback_query(TELEGRAM_TOKEN, chat_id, "stage 3")

        return False, ""

    def parse(self, command: str):
        fastapi_logger.debug(f"Get command: {command}")

    def response(self):
        pass


def answer_callback_query(token: str, callback_query_id: id, message: str):
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery?callback_query_id={callback_query_id}&text={message}"
    response = requests.get(url)
    return response.status_code == 200, response.text


def answer_callback_query_edit_message_reply_markup(
    token: str, chat_id: int, message_id: int, message: str
):
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
                {
                    "text": "haha",
                    "callback_data": "haha cmd",
                },
            ],
        ]
    }
    reply_markup = json.dumps(reply_markup)
    url = f"https://api.telegram.org/bot{token}/editMessageText?chat_id={chat_id}&message_id={message_id}&text={message}&reply_markup={reply_markup}"
    response = requests.get(url)
    return response.status_code == 200, response.text


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
