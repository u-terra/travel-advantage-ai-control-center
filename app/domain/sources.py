from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Известные платформы. Список открытый: реестр принимает любую непустую
# платформу, чтобы добавление новой (например, "youtube" или "dzen")
# не требовало правок бизнес-логики.
PLATFORM_TELEGRAM = "telegram"
PLATFORM_VK = "vk"
PLATFORM_RSS = "rss"
PLATFORM_WEB = "web"

KNOWN_PLATFORMS: frozenset[str] = frozenset(
    {PLATFORM_TELEGRAM, PLATFORM_VK, PLATFORM_RSS, PLATFORM_WEB}
)

_TELEGRAM_HOSTS = ("t.me", "telegram.me", "telegram.dog")

# Публичный Telegram-username: только латиница, цифры и подчёркивание.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


@dataclass(frozen=True)
class Source:
    """Наблюдаемый источник из реестра.

    Источник — это НЕ база знаний и НЕ источник гарантированной истины.
    Это наблюдаемый источник для поиска инфоповодов, тем для контента,
    рыночных сигналов, аргументов и возражений, историй и отзывов, идей
    для сценариев общения. Публикации источника не копируются как готовый
    контент: на их основе создаётся новый материал с ручной проверкой.
    """

    id: str
    name: str
    platform: str
    source_type: str
    purpose: str
    enabled: bool = True
    url: Optional[str] = None
    username: Optional[str] = None
    priority: int = 50
    notes: str = ""

    @property
    def is_telegram(self) -> bool:
        return self.platform == PLATFORM_TELEGRAM

    @property
    def handle(self) -> Optional[str]:
        """Telegram-username в форме `@name`. Для других платформ — None."""
        if not self.is_telegram or not self.username:
            return None
        return f"@{self.username}"

    @property
    def target(self) -> str:
        """Канонический адрес источника для кода сбора.

        Для Telegram всегда возвращается `https://t.me/<username>`, даже если
        в реестре был указан только username (или наоборот).
        """
        if self.is_telegram and self.username:
            return telegram_url(self.username)
        return self.url or ""


def is_valid_telegram_username(value: str) -> bool:
    """Похоже ли значение на публичный Telegram-username."""
    return bool(_USERNAME_RE.match(value or ""))


def normalize_telegram_username(raw: str) -> str:
    """Приводит Telegram-источник к «голому» username.

    Понимает `@name`, `name`, `t.me/name`, `https://t.me/name/`,
    `https://t.me/s/name`, а также хвост вида `?after=1` или `/123`.

    Пустая строка означает, что публичный username извлечь не удалось.
    Так же трактуется всё, что не является публичным каналом: приглашения
    (`t.me/+hash`, `t.me/joinchat/...`), чужие хосты и строки с недопустимыми
    символами. Молча «угадывать» имя канала нельзя: неверная догадка
    превратилась бы в канонический `https://t.me/<что-то-другое>`.
    """
    value = (raw or "").strip()
    if not value:
        return ""

    # Отрезаем схему, чтобы дальше работать с одним видом строки.
    for prefix in ("https://", "http://", "//"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
            break
    value = value.lstrip("@").strip()
    if not value:
        return ""

    lowered = value.lower()
    for host in _TELEGRAM_HOSTS:
        if lowered == host or lowered.startswith(host + "/"):
            value = value[len(host):].lstrip("/")
            break

    # Отбрасываем query/fragment и служебный префикс превью `s/`.
    for separator in ("?", "#"):
        value = value.split(separator, 1)[0]
    if value.lower().startswith("s/"):
        value = value[2:]

    # Приватные приглашения не дают публичного username.
    if value.startswith("+") or value.lower().startswith("joinchat/"):
        return ""

    # Из `name/123` (ссылка на конкретный пост) берём только канал.
    value = value.split("/", 1)[0]
    value = value.lstrip("@").strip()

    return value if is_valid_telegram_username(value) else ""


def telegram_url(username: str) -> str:
    """Канонический публичный URL Telegram-источника."""
    normalized = normalize_telegram_username(username)
    return f"https://t.me/{normalized}" if normalized else ""
