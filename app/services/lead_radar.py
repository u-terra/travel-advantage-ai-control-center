from __future__ import annotations

import importlib.util
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Optional

log = logging.getLogger(__name__)


# Итоговый показ количества материалов не должен быть жёстко "1+1": он растёт
# вместе с суммой квот ниже (см. _ACTION_QUOTA). Telegram и Web обязаны звать
# build_workspace_signals(..., limit=DISPLAY_LIMIT) — один и тот же импортируемый
# символ, а не повторённое число в двух местах, иначе они могут незаметно разойтись.
DISPLAY_LIMIT = 10
_MAX_LIMIT = max(10, DISPLAY_LIMIT)
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

# Свежесть по смыслу типа сигнала — вопрос клиента "протухает" за часы,
# рыночная новость остаётся релевантной дольше, контентная тема — ещё дольше.
# ai_score здесь не участвует: это константа на категорию (lead=70/market=45/
# content=32 у всей продакшен-истории), а не оценка качества конкретной записи.
_ACTION_FRESHNESS_HOURS: dict[str, float] = {
    "careful_reply": 72.0,
    "observe": 24.0 * 7,
    "content": 24.0 * 14,
}

# Квота итоговой выдачи по типу сигнала. Отсутствующая категория НЕ
# добивается другой — итог может быть короче суммы квот.
#
# Сумма квот (3+3+5=11) НАМЕРЕННО больше DISPLAY_LIMIT (10): это не баг, а
# осознанный компромисс минимальной реализации. Если все три bucket'а разом
# заполнены до квоты, финальный срез build_workspace_signals() по DISPLAY_LIMIT
# обрежет ровно один элемент — самый старый из bucket'а "content" (он идёт
# последним по _ACTION_PRIORITY). См. test_overall_cap_trims_last_content_item
# в tests/test_lead_radar.py и раздел про ranking в отчёте: если это поведение
# нежелательно, следующий шаг — либо поднять DISPLAY_LIMIT до 11, либo снизить
# квоту content до 4, а не менять сортировку.
_ACTION_QUOTA: dict[str, int] = {
    "careful_reply": 3,
    "observe": 3,
    "content": 5,
}

# Категория из Lead Radar (`ai_category`), которую вообще не показываем.
_NOISE_CATEGORY = "noise"

# Человекочитаемый тип сигнала по его категории. Для незнакомых категорий
# остаётся нейтральный fallback — без агрессивных формулировок.
_CATEGORY_LABELS: dict[str, str] = {
    "lead_signal": "🎯 Вопрос клиента",
    "content_signal": "💡 Тема для контента",
    "market_signal": "👀 Наблюдать рынок",
}

_CATEGORY_FALLBACK = "🔹 Сигнал интереса"

_ROUTE_CARD = (
    "📡 Сигналы интереса\n"
    "\n"
    "Источник: мониторинг подключённых источников\n"
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


def _hours_old(created_at: object) -> Optional[float]:
    """Сколько часов прошло с created_at, или None если дату не разобрать."""
    raw = str(created_at or "").strip()
    if not raw:
        return None
    normalized = raw.replace(" ", "T", 1) if "T" not in raw else raw
    try:
        created = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() / 3600.0


def _is_within_action_freshness(action: str, created_at: object) -> bool:
    """Свежесть по смыслу типа сигнала (см. _ACTION_FRESHNESS_HOURS)."""
    limit_hours = _ACTION_FRESHNESS_HOURS.get(action)
    if limit_hours is None:
        return True
    hours = _hours_old(created_at)
    if hours is None:
        return False
    return hours <= limit_hours


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


# ── Content-bucket ranking (только action == "content") ──────────────────────
# Раньше content-bucket сортировался только по created_at DESC, поэтому более
# свежая, но слабая публикация или товарная реклама могла оказаться выше
# действительно полезного travel-материала. Ниже — небольшой deterministic
# вторичный ключ по item_title + item_summary (НЕ ai_score — он константа на
# категорию, не оценка качества; НЕ LLM). Ничего не дропается: даже сигнал
# самого низкого тира остаётся кандидатом и конкурирует за квоту, просто после
# более высокотировых — см. ORCHESTRAVEL: ranking внутри content bucket.

_CONTENT_TIER_PRODUCT_AD = 0  # товарная реклама без travel-пользы
_CONTENT_TIER_WEAK = 1        # абстрактный lifestyle без конкретной пользы
_CONTENT_TIER_DEFAULT = 2     # обычная конкретная travel-тема
_CONTENT_TIER_STRONG = 3      # практическая инструкция/маршрут/чек-лист

# Сильные практические сигналы — явные how-to/гид/чек-лист формулировки.
_CONTENT_STRONG_MARKERS: tuple[str, ...] = (
    "как добраться", "как доехать", "что взять с собой", "что взять в поездку",
    "чек-лист", "чеклист", "список вещей",
    "маршрут по", "маршрут выходного дня", "туристический маршрут",
    "путеводит", "куда сходить", "что посмотреть", "необычные места",
    "советы туристам", "инструкция для туриста",
)
# "Аэропорт" сам по себе ни о чём не говорит (может быть про авиакатастрофу),
# но "аэропорт" + практическая транспортная связка — это гид "как добраться".
_AIRPORT_TRANSPORT_WORDS: tuple[str, ...] = (
    "транспорт", "центр", "автобус", "метро", "трансфер", "вокзал", "такси",
)

# Полезные конкретные travel-темы без формального how-to: события, опыт
# туристов, необычные факты — не дотягивают до "сильных", но точно не слабые
# и не должны провалиться в weak-tier только из-за короткого текста (пример D:
# "мыс Четырёх скал" не должен проваливаться из-за отсутствия "как добраться").
_CONTENT_GOOD_TOPIC_MARKERS: tuple[str, ...] = (
    "фестивал", "событие в", "необычные факты", "необычный факт",
    "интересный факт", "типичные ошибки", "жалобы туристов",
    "нелепые жалобы", "смешные жалобы", "опыт туристов", "личный опыт",
)

# Абстрактный lifestyle-клишированный текст без конкретной пользы (места,
# совета, маршрута) — короткая эмоциональная фраза типа "Красота северного
# леса." Длина — намеренно низкий порог: реальный travel-контент почти всегда
# длиннее одной эмоциональной фразы.
_CONTENT_WEAK_MARKERS: tuple[str, ...] = (
    "невероятная красота", "потрясающие виды", "волшебная атмосфера",
    "заряжает энергией", "дарит вдохновение", "вдохновляет",
    "трогает до глубины души", "просто красота", "какая красота",
)
_CONTENT_WEAK_MAX_LEN = 40

# Товарная реклама гаджетов — самый низкий тир, но НЕ drop: пограничный
# случай (гид со спонсорской интеграцией) защищён тем, что _CONTENT_STRONG_MARKERS
# проверяются раньше и выигрывают, если реально есть практическая польза.
_CONTENT_PRODUCT_AD_MARKERS: tuple[str, ...] = (
    "смартфон", "смарт-часы", "смарт часы", "фитнес-браслет", "фитнес браслет",
    "наушники", "ноутбук", "гаджет", "gps-трек", "gps трек", "трекер",
    "на правах рекламы", "партнёрский материал", "промокод",
)


def _content_quality_rank(title: str, summary: str) -> int:
    """Deterministic tier для сортировки content-bucket. Выше — лучше.

    Использует item_title + item_summary целиком (не только title и не
    action_reason). Не использует ai_score. Не вызывает LLM.
    """
    text = f"{title or ''} {summary or ''}".lower()

    has_airport_transport = "аэропорт" in text and any(
        word in text for word in _AIRPORT_TRANSPORT_WORDS
    )
    if has_airport_transport or any(marker in text for marker in _CONTENT_STRONG_MARKERS):
        return _CONTENT_TIER_STRONG

    if any(marker in text for marker in _CONTENT_PRODUCT_AD_MARKERS):
        return _CONTENT_TIER_PRODUCT_AD

    if any(marker in text for marker in _CONTENT_GOOD_TOPIC_MARKERS):
        return _CONTENT_TIER_DEFAULT

    if len(text.strip()) < _CONTENT_WEAK_MAX_LEN or any(
        marker in text for marker in _CONTENT_WEAK_MARKERS
    ):
        return _CONTENT_TIER_WEAK

    return _CONTENT_TIER_DEFAULT


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


def build_workspace_signals(
    config: LeadRadarConfig, records, *, limit: int = _DEFAULT_LIMIT
) -> Optional[list[LeadSignal]]:
    """Build display signals only from already workspace-authorized records."""
    try:
        recommender = _load_recommender(config)
    except Exception:
        log.warning("lead_radar: failed to load action_recommender")
        return None
    recommend_action = getattr(recommender, "recommend_action", None)
    action_label_fn = getattr(recommender, "action_label", None)
    if not callable(recommend_action) or not callable(action_label_fn):
        return None

    by_action: dict[str, list[LeadSignal]] = {action: [] for action in _ACTION_PRIORITY}
    # id -> content quality tier. Отдельная структура, а не поле LeadSignal:
    # ранг считаем из record.item_summary, которого в публичном dataclass нет
    # и добавлять незачем — ключ по id достаточен только для финальной
    # сортировки content-bucket ниже.
    content_quality: dict[int, int] = {}
    for record in records:
        row = {
            "created_at": record.raw_created_at,
            "source_type": record.source_type,
            "origin_type": record.origin_type,
            "ai_score": record.ai_score,
            "ai_category": record.ai_category,
            "ai_reason": record.ai_reason,
            "item_title": record.item_title,
            "item_summary": record.item_summary,
            "item_url": record.item_url,
        }
        if not _is_allowed_row(row):
            continue
        try:
            info = recommend_action(row)
        except Exception:
            continue
        action = (info or {}).get("recommended_action") or ""
        if action not in _ACTION_PRIORITY:
            continue
        if not _is_within_action_freshness(action, record.raw_created_at):
            continue
        try:
            label = action_label_fn(action) or action
        except Exception:
            label = action
        if action == "content":
            content_quality[record.interpretation_id] = _content_quality_rank(
                record.item_title, record.item_summary
            )
        by_action[action].append(LeadSignal(
            id=record.interpretation_id,
            created_at=record.raw_created_at,
            source_type=record.source_type,
            score=_to_float(record.ai_score),
            category=record.ai_category or "",
            title=record.item_title,
            url=record.item_url,
            recommended_action=action,
            action_label=str(label),
            action_reason=str((info or {}).get("action_reason") or "").strip(),
        ))

    # Квота на bucket, без добивки отсутствующей категории другой. ai_score
    # здесь намеренно не участвует (см. _ACTION_FRESHNESS_HOURS выше). Для
    # careful_reply/observe единственный осмысленный критерий сейчас —
    # свежесть. Для content — сначала quality tier (см. _content_quality_rank
    # выше), внутри одного тира — та же свежесть как tie-breaker.
    signals: list[LeadSignal] = []
    for action in sorted(_ACTION_PRIORITY, key=_ACTION_PRIORITY.get):
        if action == "content":
            bucket = sorted(
                by_action[action],
                key=lambda signal: (
                    content_quality.get(signal.id, _CONTENT_TIER_DEFAULT),
                    signal.created_at,
                ),
                reverse=True,
            )
        else:
            bucket = sorted(by_action[action], key=lambda signal: signal.created_at, reverse=True)
        signals.extend(bucket[: _ACTION_QUOTA.get(action, limit)])
    return signals[: max(1, min(limit, _MAX_LIMIT))]


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


# Человекочитаемое «почему стоит обратить внимание» по типу рекомендованного
# действия — используется только когда action_reason (от action_recommender)
# пуст. Ничего не придумывает про конкретный сигнал, только нейтральная
# формулировка по категории — сам score/source_type сюда никогда не попадают.
_WHY_FALLBACK: dict[str, str] = {
    "content": "Тема перекликается с интересами вашей аудитории и может стать поводом для поста.",
    "observe": "Похоже, эта тема сейчас активно обсуждается на рынке — стоит держать её в поле зрения.",
    "careful_reply": "Похоже на вопрос от потенциального клиента — стоит ответить лично.",
}
_WHY_DEFAULT_FALLBACK = "Сигнал может быть полезен для вашей аудитории."

# Компактная редакционная подсказка для content-сигналов. Без данных сверх
# заголовка/причины сигнала это не может быть уникальным фактом про источник —
# это подсказка по подаче, а не утверждение о содержании источника.
_CONTENT_ANGLE_HINT = (
    "Свяжите тему с вашим направлением и добавьте личный пример или мнение."
)


def category_label(category: str) -> str:
    """Человекочитаемый тип сигнала по категории Lead Radar (`ai_category`).

    Знакомые категории получают заданную подпись, для остальных остаётся
    нейтральный fallback.
    """
    key = (category or "").strip().lower()
    return _CATEGORY_LABELS.get(key, _CATEGORY_FALLBACK)


def why_text(signal: LeadSignal) -> str:
    """«Почему стоит обратить внимание» — человеческая формулировка без
    технического score/source_type. action_reason уже приходит человекочитаемым
    от action_recommender; пустой action_reason не заменяется выдуманной
    статистикой, только нейтральным fallback по типу действия.

    Публичная (без ведущего подчёркивания): единственный источник этого текста
    и для Telegram (build_summary → _format_signal_block), и для Web
    (/api/signals) — чтобы не завести вторую, отдельную формулировку для Web.
    """
    reason = (signal.action_reason or "").strip()
    if reason:
        return reason
    return _WHY_FALLBACK.get(signal.recommended_action, _WHY_DEFAULT_FALLBACK)


def content_angle_hint() -> str:
    """«Как можно подать» для content-сигналов — тот же текст, что уже видит
    Telegram (см. _format_signal_block). Единственный источник для обоих
    интерфейсов, чтобы Web не завёл свою собственную формулировку."""
    return _CONTENT_ANGLE_HINT


def _format_signal_block(signal: LeadSignal, index: int) -> str:
    header = category_label(signal.category)
    title = _truncate(signal.title or "(без заголовка)", 110)
    why = _truncate(why_text(signal), 160)
    lines = [
        f"{index}. {header}",
        "",
        "Тема:",
        title,
        "",
        "Почему стоит обратить внимание:",
        why,
    ]
    if signal.recommended_action == "content":
        lines.extend([
            "",
            "Как можно подать:",
            _truncate(_CONTENT_ANGLE_HINT, 160),
        ])
    lines.extend(["", signal.url or "—"])
    return "\n".join(lines)


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
