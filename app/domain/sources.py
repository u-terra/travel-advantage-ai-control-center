from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

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

# Разговорные названия той же платформы. Нужны только на входе: владелец пишет
# «website» или «url», а в реестре по-прежнему хранится одно значение `web`.
# Два имени для одной платформы разошлись бы в фильтрах by_platform().
PLATFORM_ALIASES: Mapping[str, str] = {
    "website": PLATFORM_WEB,
    "site": PLATFORM_WEB,
    "url": PLATFORM_WEB,
    "http": PLATFORM_WEB,
    "https": PLATFORM_WEB,
    "tg": PLATFORM_TELEGRAM,
    "vkontakte": PLATFORM_VK,
    "feed": PLATFORM_RSS,
}

# Назначение источника. Список тоже открытый: в реестре уже живут более
# подробные значения (`travel_content_and_market_signals` и т.п.), а эти
# четыре — нейтральные «корзины» для источников, содержание которых ещё
# не разобрано.
PURPOSE_CONTENT = "content"
PURPOSE_MARKET = "market"
PURPOSE_REVIEWS = "reviews"
PURPOSE_MIXED = "mixed"

KNOWN_PURPOSES: frozenset[str] = frozenset(
    {PURPOSE_CONTENT, PURPOSE_MARKET, PURPOSE_REVIEWS, PURPOSE_MIXED}
)

_TELEGRAM_HOSTS = ("t.me", "telegram.me", "telegram.dog")
_VK_HOSTS = ("vk.com", "m.vk.com", "vk.ru")

_ALLOWED_URL_SCHEMES = ("http", "https")

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

    Общие свойства источника — поля этого класса. Всё, что нужно только
    конкретному сборщику (лимиты, фильтры, идентификаторы платформы),
    живёт отдельно в `collector` и реестром не интерпретируется.
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
    collector: Mapping[str, Any] = field(default_factory=dict)
    # Пустая строка означает «неизвестно»: источники, заведённые до появления
    # отметок времени, ими не заполняются задним числом — выдуманная дата
    # выглядела бы как настоящая.
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_telegram(self) -> bool:
        return self.platform == PLATFORM_TELEGRAM

    def collector_setting(self, key: str, default: Any = None) -> Any:
        """Настройка сборщика по имени. Реестр её смысл не толкует."""
        return self.collector.get(key, default)

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

    @property
    def identity_key(self) -> str:
        """Ключ «это тот же самый источник» для поиска дубликатов.

        Один и тот же канал можно записать по-разному (`@name`, `t.me/name`,
        `https://t.me/name/42`), поэтому сравнивать сырые строки нельзя.
        Ключ строится из платформы и нормализованного адреса.
        """
        return source_identity_key(
            platform=self.platform, url=self.url, username=self.username
        )


def source_identity_key(
    *, platform: str, url: Optional[str], username: Optional[str]
) -> str:
    key = (platform or "").strip().lower()
    if key == PLATFORM_TELEGRAM:
        normalized = normalize_telegram_username(username or url or "")
        return f"{key}:{normalized.lower()}" if normalized else ""
    normalized_url = normalize_url(url or "")
    if normalized_url:
        return f"{key}:{normalized_url}"
    handle = (username or "").strip().lower()
    return f"{key}:@{handle}" if handle else ""


def normalize_url(raw: str) -> str:
    """Приводит обычный адрес к каноническому виду.

    Пустая строка означает «это не пригодный для реестра http(s)-адрес».
    Отклоняются схемы кроме http/https (`tg://`, `javascript:`, `file:`),
    адреса без хоста и адреса с логином/паролем в URL — секретам в реестре
    источников не место.

    Query и fragment сохраняются: `.../#features` — осмысленная часть ссылки,
    и молча её отрезать значило бы подменить адрес, который дал владелец.
    """
    value = (raw or "").strip()
    if not value or any(char.isspace() for char in value):
        return ""

    try:
        parts = urlsplit(value)
    except ValueError:
        return ""

    if parts.scheme.lower() not in _ALLOWED_URL_SCHEMES:
        return ""
    if "@" in parts.netloc:  # user:password@host
        return ""

    host = (parts.hostname or "").strip().lower()
    if not host or "." not in host:
        return ""

    netloc = host
    try:
        port = parts.port
    except ValueError:
        return ""
    if port is not None:
        netloc = f"{host}:{port}"

    path = parts.path
    if path == "/":
        path = ""

    rebuilt = f"{parts.scheme.lower()}://{netloc}{path}"
    if parts.query:
        rebuilt += f"?{parts.query}"
    if parts.fragment:
        rebuilt += f"#{parts.fragment}"
    return rebuilt


def url_host(raw: str) -> str:
    """Хост адреса в нижнем регистре. Схему можно не указывать."""
    value = (raw or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value.lstrip('/')}"
    try:
        return (urlsplit(value).hostname or "").strip().lower()
    except ValueError:
        return ""


def normalize_platform(raw: str) -> str:
    """Название платформы в каноническом виде (с учётом алиасов)."""
    key = (raw or "").strip().lower()
    return PLATFORM_ALIASES.get(key, key)


def detect_platform(raw: str) -> str:
    """Определяет платформу по адресу. Пустая строка — адрес не распознан.

    Догадки строятся только по хосту, а не по форме строки: `http://intranet/x`
    внешне похож на Telegram-username, но им не является.
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if value.startswith("@"):
        return PLATFORM_TELEGRAM if normalize_telegram_username(value) else ""

    host = url_host(value)
    if host in _TELEGRAM_HOSTS:
        return PLATFORM_TELEGRAM
    if host in _VK_HOSTS:
        return PLATFORM_VK
    return PLATFORM_WEB if normalize_url(value) else ""


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
