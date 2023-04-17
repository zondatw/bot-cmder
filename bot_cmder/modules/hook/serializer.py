from typing import List

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
    last_name: str
    is_bot: bool
    language_code: str
    username: str


class Message(BaseModel):
    chat: Chat
    date: int
    text: str
    message_id: int
    entities: List[Entry] | None = []
    from_: From

    class Config:
        fields = {"from_": "from"}


class HookRequest(BaseModel):
    update_id: int
    message: Message
