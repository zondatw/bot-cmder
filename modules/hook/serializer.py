from pydantic import BaseModel


class HookResponse(BaseModel):
    ok: bool
    result: bool
    description: str
