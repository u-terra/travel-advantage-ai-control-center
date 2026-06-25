from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import Message


class IsAdmin(BaseFilter):
    def __init__(self, admin_id: int) -> None:
        self.admin_id = admin_id

    async def __call__(self, message: Message) -> bool:
        user = message.from_user
        return user is not None and user.id == self.admin_id
