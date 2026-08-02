from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove, TelegramObject

# Единственный ответ постороннему пользователю. Не раскрывает меню, данные,
# статистику, результаты поиска, технические ошибки и сведения о конфигурации.
ACCESS_DENIED_MESSAGE = "Доступ к панели управления ограничен."


def parse_allowed_user_ids(raw: str | None) -> frozenset[int]:
    """Разбирает список разрешённых Telegram ID из строки окружения.

    Поддерживает разделители «,» и «;». Пустая или отсутствующая строка даёт
    пустой набор — доступ закрыт по умолчанию. Нечисловые фрагменты игнорируются.
    """
    if not raw:
        return frozenset()

    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            continue
    return frozenset(ids)


class AllowlistMiddleware(BaseMiddleware):
    """Централизованный guard доступа к панели управления.

    Регистрируется как outer-middleware на наблюдателях message и callback_query,
    поэтому срабатывает раньше любых фильтров и хендлеров — посторонний не видит
    ни меню, ни данных, ни ошибок. Доступ закрыт по умолчанию: если набор
    разрешённых ID пуст или ID пользователя в него не входит — обработка не
    доходит до хендлеров панели.
    """

    def __init__(self, allowed_user_ids: frozenset[int]) -> None:
        self.allowed_user_ids = allowed_user_ids

    def is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.allowed_user_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        user_id = getattr(user, "id", None)

        if self.is_allowed(user_id):
            return await handler(event, data)

        await self._deny(event)
        return None

    async def _deny(self, event: TelegramObject) -> None:
        if isinstance(event, CallbackQuery):
            # Всплывающий алерт, ничего из содержимого сообщения не раскрывается.
            await event.answer(ACCESS_DENIED_MESSAGE, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(
                ACCESS_DENIED_MESSAGE,
                reply_markup=ReplyKeyboardRemove(),
            )
