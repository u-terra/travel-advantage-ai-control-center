from __future__ import annotations

import importlib.util
import logging
import sqlite3
from datetime import date, timedelta
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Optional

log = logging.getLogger(__name__)


_MAX_LIMIT = 8
_DEFAULT_LIMIT = 5
_FETCH_BATCH = 200
_FRESH_DAYS = 30

_TEST_URL_FRAGMENT = "vk.com/test-"

_TEST_TEXT_MARKERS = (
    "test-batch",
    "test-safe",
    "тестовая запись",
    "тестовый сигнал",
)

_IRRELEVANT_FINANCE_MARKERS = (
    "credit card",
    "credit cards",
    "cashback",
    "cash back",
    "rewards",
    "points",
    "chase",
    "capital one",
    "visa signature",
    "visa card",
    "mastercard",
    "american express",
    "amex",
    "bank bonus",
    "bank rewards",
    "banking",
)

_ACTION_PRIORITY: dict[str, int] = {
    "careful_reply": 0,
    "observe": 1,
    "content": 2,
}

# Категория из Lead Radar (`ai_category`), которую вообще не показываем.
_NOISE_CATEGORY = "noise"

# Человекочитаемый тип сигнала по его категории. Для незнакомых категорий
# остаётся нейтральный fallback — без агрессивных формулировок.
_CATEGORY_LABELS: dict[str, str] = {
    "content_signal": "💡 Тема для контента",
    "market_signal": "👀 Наблюдать рынок",
}

_CATEGORY_FALLBACK = "🔹 Сигнал интереса"

_ROUTE_CARD = (
    "📡 Сигналы интереса\n"
    "\n"
    "Источник: Travel Lead Radar\n"
    "Режим: готовая выборка из базы\n"
    "Автоматических сообщений никому не отправляется."
)

_EMPTY_SUMMARY = (
    "Подходящих сигналов пока нет.\n"
    "Радар ничего не отправлял и не запускал новый мониторинг."
)

_UNAVAILABLE_SUMMARY = (
    "Travel Lead Radar сейчас недоступен.\n"
    "Ничего не было отправлено и не запускалось."
)


@dataclass(frozen=True)
class LeadRadarConfig:
    """Доступ к локальной SQLite-базе Travel Lead Radar.

    Внутренний HTTP-сервис не используется: TA Control Center и Lead Radar
    живут на одном VPS, и здесь мы читаем `leads.db` напрямую через sqlite3.

    Поле `recommender_path` опционально и нужно только для офлайн-тестов —
    в продакшене путь к `action_recommender.py` выводится из `db_path`.
    """
    db_path: Path
    recommender_path: Optional[Path] = None

    @property
    def is_configured(self) -> bool:
        return bool(str(self.db_path))


@dataclass(frozen=True)
class LeadSignal:
    id: int
    created_at: str
    source_type: str
    score: Optional[float]
    category: str
    title: str
    url: str
    recommended_action: str
    action_label: str
    action_reason: str


# ── Загрузка существующего action_recommender.py из проекта Lead Radar ───────
#
# Чтобы не дублировать логику рекомендатора, грузим его модуль один раз через
# importlib.util и держим в кеше. Ключ кеша — реальный путь к файлу.

_RECOMMENDER_CACHE: dict[str, object] = {}
_RECOMMENDER_LOCK = Lock()


def _derive_recommender_path(db_path: Path) -> Path:
    # /opt/travel_lead_radar/data/leads.db
    # -> /opt/travel_lead_radar/app/ai/action_recommender.py
    return db_path.parent.parent / "app" / "ai" / "action_recommender.py"


def _load_recommender(config: LeadRadarConfig):
    path = config.recommender_path or _derive_recommender_path(config.db_path)
    key = str(path.resolve())
    with _RECOMMENDER_LOCK:
        cached = _RECOMMENDER_CACHE.get(key)
        if cached is not None:
            return cached
        if not path.is_file():
            raise FileNotFoundError(f"recommender file not found: {path}")
        spec = importlib.util.spec_from_file_location(
            f"_lead_radar_recommender_{abs(hash(key))}", str(path)
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot build spec for: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _RECOMMENDER_CACHE[key] = module
        return module


def _is_fresh(created_at: object) -> bool:
    """Проверяет, что запись не старше 30 календарных дней."""
    raw = str(created_at or "").strip()
    try:
        created_date = date.fromisoformat(raw[:10])
    except ValueError:
        return False

    return created_date >= date.today() - timedelta(days=_FRESH_DAYS)


def _row_text(row: dict[str, object]) -> str:
    """Собирает доступный текст записи для локального фильтра шума."""
    fields = (
        "item_title",
        "item_summary",
        "item_url",
        "ai_reason",
        "source_type",
    )
    return " ".join(str(row.get(field) or "") for field in fields).lower()


def _is_allowed_row(row: dict[str, object]) -> bool:
    """Отсекает шумовые, устаревшие, тестовые и нерелевантные записи."""
    if not _is_fresh(row.get("created_at")):
        return False

    if str(row.get("ai_category") or "").strip().lower() == _NOISE_CATEGORY:
        return False

    url = str(row.get("item_url") or "").lower()
    text = _row_text(row)

    if _TEST_URL_FRAGMENT in url:
        return False

    if any(marker in text for marker in _TEST_TEXT_MARKERS):
        return False

    if any(marker in text for marker in _IRRELEVANT_FINANCE_MARKERS):
        return False

    return True


# ── Чтение сигналов ──────────────────────────────────────────────────────────


def fetch_signals_sync(
    config: LeadRadarConfig, *, limit: int = _DEFAULT_LIMIT
) -> Optional[list[LeadSignal]]:
    """Синхронно читает свежие сигналы из локальной SQLite Lead Radar.

    Возвращает список (возможно пустой) при успехе и None при любой ошибке:
    база недоступна, sqlite-ошибка, рекомендатор не загрузился. Технические
    детали наружу не пробрасываются.

    В БД ничего не пишется. Никаких сетевых вызовов.
    """
    if not config.is_configured:
        return None

    if limit < 1:
        limit = 1
    if limit > _MAX_LIMIT:
        limit = _MAX_LIMIT

    db_path = Path(config.db_path)
    if not db_path.is_file():
        log.warning("lead_radar: db not found")
        return None

    try:
        recommender = _load_recommender(config)
    except Exception:
        log.warning("lead_radar: failed to load action_recommender")
        return None

    recommend_action = getattr(recommender, "recommend_action", None)
    action_label_fn = getattr(recommender, "action_label", None)
    if not callable(recommend_action) or not callable(action_label_fn):
        log.warning("lead_radar: recommender missing required functions")
        return None

    try:
        # uri=True + mode=ro: открываем строго в read-only режиме —
        # дополнительная гарантия, что мы ничего не пишем в leads.db.
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=2.0
        )
    except sqlite3.Error:
        log.warning("lead_radar: cannot open db")
        return None

    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, created_at, source_type, ai_score, ai_category, "
            "item_title, item_summary, item_url, ai_reason "
            "FROM lead_signals "
            "ORDER BY datetime(created_at) DESC LIMIT ?",
            (_FETCH_BATCH,),
        ).fetchall()
    except sqlite3.Error:
        log.warning("lead_radar: query failed")
        return None
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    actionable: list[LeadSignal] = []
    for row in rows:
        d = dict(row)
        if not _is_allowed_row(d):
            continue

        try:
            info = recommend_action(d)
        except Exception:
            log.warning("lead_radar: recommender raised on a row")
            continue
        action = (info or {}).get("recommended_action") or ""
        if action not in _ACTION_PRIORITY:
            continue
        reason = (info or {}).get("action_reason") or ""
        try:
            label = action_label_fn(action) or action
        except Exception:
            label = action
        actionable.append(
            LeadSignal(
                id=int(d.get("id") or 0),
                created_at=str(d.get("created_at") or ""),
                source_type=str(d.get("source_type") or ""),
                score=_to_float(d.get("ai_score")),
                category=str(d.get("ai_category") or ""),
                title=str(d.get("item_title") or "").strip(),
                url=str(d.get("item_url") or ""),
                recommended_action=action,
                action_label=str(label),
                action_reason=str(reason).strip(),
            )
        )

    # Стабильная двухпроходная сортировка: внутри группы — новее первым.
    actionable.sort(key=lambda s: s.created_at, reverse=True)
    actionable.sort(key=lambda s: _ACTION_PRIORITY[s.recommended_action])

    return actionable[:limit]


def _to_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── Форматирование Telegram-сводки ───────────────────────────────────────────


def _truncate(text: str, max_len: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _format_date(created_at: str) -> str:
    s = (created_at or "").strip()
    if len(s) >= 10:
        return s[:10]
    return s or "—"


def _format_score(score: Optional[float]) -> str:
    if score is None:
        return "—"
    if score == int(score):
        return str(int(score))
    return f"{score:.1f}"


def _format_source(source_type: str) -> str:
    s = (source_type or "").strip()
    if not s:
        return "—"
    if s.lower() in {"rss", "vk"}:
        return s.upper()
    return s


def category_label(category: str) -> str:
    """Человекочитаемый тип сигнала по категории Lead Radar (`ai_category`).

    Знакомые категории получают заданную подпись, для остальных остаётся
    нейтральный fallback.
    """
    key = (category or "").strip().lower()
    return _CATEGORY_LABELS.get(key, _CATEGORY_FALLBACK)


def _format_signal_block(signal: LeadSignal, index: int) -> str:
    header = category_label(signal.category)
    title = _truncate(signal.title or "(без заголовка)", 110)
    meta = (
        f"{_format_source(signal.source_type)} · score "
        f"{_format_score(signal.score)} · {_format_date(signal.created_at)}"
    )
    reason = _truncate(signal.action_reason or "—", 140)
    url = signal.url or "—"
    return (
        f"{index}. {header}\n"
        f"{title}\n"
        f"{meta}\n"
        f"Почему: {reason}\n"
        f"{url}"
    )


def build_summary(signals: list[LeadSignal]) -> str:
    """Компактная сводка для одного Telegram-сообщения (≤ 4096 символов)."""
    if not signals:
        return _EMPTY_SUMMARY

    review_signals = [
        signal
        for signal in signals
        if signal.recommended_action in {"careful_reply", "observe"}
    ]
    content_signals = [
        signal
        for signal in signals
        if signal.recommended_action == "content"
    ]

    parts: list[str] = []

    if review_signals:
        blocks = [
            _format_signal_block(signal, index)
            for index, signal in enumerate(review_signals, start=1)
        ]
        parts.append("👀 Сигналы для просмотра\n\n" + "\n\n".join(blocks))
    else:
        parts.append(
            "👀 Сигналы для просмотра\n\nПодходящих сигналов пока нет."
        )

    if content_signals:
        blocks = [
            _format_signal_block(signal, index)
            for index, signal in enumerate(content_signals, start=1)
        ]
        parts.append("💡 Идеи для контента\n\n" + "\n\n".join(blocks))

    return "\n\n".join(parts)


def route_card() -> str:
    return _ROUTE_CARD


def empty_summary() -> str:
    return _EMPTY_SUMMARY


def unavailable_summary() -> str:
    return _UNAVAILABLE_SUMMARY
