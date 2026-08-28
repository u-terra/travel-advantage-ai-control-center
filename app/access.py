from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

# Исторический текст отказа — Stage 3A больше не показывает его пользователю
# автоматически (см. AllowlistMiddleware ниже), константа оставлена для
# обратной совместимости импортов и как задокументированная формулировка,
# если она понадобится где-то явно в будущем.
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
    """Stage 3A: публичный вход ≠ рабочий доступ.

    До Stage 3A это был единственный guard доступа к боту вообще — не в
    allowlist означало полный отказ ("Доступ к панели управления
    ограничен."). Теперь публичный слой (/start, лобби, "Осмотреться")
    обязан быть доступен любому Telegram-пользователю без allowlist, а
    рабочий доступ решает AccessStateMiddleware (app/access_state_gate.py)
    по workspace/подписке, а не по этому env-списку.

    AllowlistMiddleware больше никого не блокирует — он только кладёт
    is_allowlisted в workflow data как явный, изолированный флаг:
    legacy/admin/pilot-разработка может явно проверить его там, где это
    осознанно нужно, вместо того чтобы этот guard молча решал за всех.
    TELEGRAM_ALLOWED_USER_IDS сознательно не удалён — см. app/config.py.
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
        data["is_allowlisted"] = self.is_allowed(user_id)
        return await handler(event, data)
