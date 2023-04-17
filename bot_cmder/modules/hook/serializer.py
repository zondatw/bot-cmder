from typing import List, Union

from pydantic import BaseModel


class HookResponse(BaseModel):
    ok: bool
    result: bool
    description: str


class Chat(BaseModel):
    id: int
    first_name: str
    last_name: str
    type: str
    username: str


class Entry(BaseModel):
    length: int
    offset: int
    type: str


class From(BaseModel):
    id: int
    first_name: str
    last_name: Union[str, None]
    is_bot: bool
    language_code: Union[str, None]
    username: str


class InlineKeyboardData(BaseModel):
    callback_data: str
    text: str


class ReplyMarkup(BaseModel):
    inline_keyboard: List[List[InlineKeyboardData]] | None = []


class Message(BaseModel):
    chat: Chat
    date: int
    text: str
    message_id: int
    entities: List[Entry] | None = []
    from_: From
    reply_markup: ReplyMarkup | None = {}

    class Config:
        fields = {"from_": "from"}


class CallbackQuery(BaseModel):
    id: str
    chat_instance: str
    data: str
    from_: From
    message: Message

    class Config:
        fields = {"from_": "from"}


class HookRequest(BaseModel):
    update_id: int
    message: Message | None = None
    callback_query: CallbackQuery | None = None
