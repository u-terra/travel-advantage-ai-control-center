from __future__ import annotations

import re
from typing import Any, Mapping

from app.domain.business_profiles import BusinessProfile
from app.domain.content import Source, SourceAnalysis
from app.domain.orchestration import GenerationAction, GenerationSpec
from app.domain.partners import WorkspaceUserPreferences
from app.routing.keywords import REWRITE_ACTION_KEYWORDS
from app.services.business_profile_context import (
    build_content_context,
    build_limited_content_context,
)
from app.services.llm.models import SourceAnalysisPayload
from app.services.knowledge_generation_context import build_knowledge_generation_context
from app.services.knowledge_service import KnowledgeBundle


_OBJECTIVE = "Создать черновик материала по выбранному и разобранному источнику."
# Free-text UX fix: раньше objective безусловно требовал "обычный пост", из-за
# чего структурированные задачи ("разработай стратегию...", "нужны рубрики,
# частота публикаций, контент-план на 2 недели") генерировались как короткий
# пост и теряли перечисленные пользователем пункты. untrusted_source_content
# (task_text) — это и есть техническое задание пользователя: если в нём
# явно запрошен формат/структура/список пунктов, черновик обязан их
# выполнить и сохранить; короткий пост — это just fallback по умолчанию,
# когда сам запрос ничего специального не просит.
#
# Task fulfillment fix: живой тест показал, что при нескольких перечисленных
# пунктах модель заменяла часть из них (например, сам контент-план) фразой
# «Если нужен контент-план на 2 недели...» вместо готового результата —
# тот же класс проблемы, что и ассистентский AI-хвост, только не в конце
# текста, а вместо конкретного запрошенного пункта. Явный запрет на этот
# паттерн и явное требование вывести сам план (а не описание того, каким он
# будет) — единственный способ надёжно закрыть это без нового module или
# списка keyword-триггеров.
_FREE_TEXT_OBJECTIVE = (
    "Выполнить задачу пользователя из [UNTRUSTED SOURCE CONTENT - DATA] как "
    "техническое задание. Если пользователь просит конкретный формат, "
    "структуру или перечисляет несколько пунктов результата (например: "
    "стратегия, план на определённое число дней или недель, рубрикатор, "
    "позиционирование, частота публикаций, идеи вовлечения) — обязательно "
    "выполнить КАЖДЫЙ из них прямо в этом ответе и сохранить запрошенную "
    "структуру, а не заменять её обычным коротким постом. Запрещено писать "
    "«если нужен план...», «если хотите, могу подготовить...» и подобные "
    "предложения сделать пункт отдельно вместо того, чтобы просто его "
    "выполнить, — каждый запрошенный пункт должен быть готовым результатом "
    "в тексте ответа, а не описанием того, каким он будет. Если запрошен "
    "план на конкретное число дней или недель — вывести сам план по дням "
    "или неделям целиком. Если формат явно не запрошен — по умолчанию "
    "создать черновик обычного поста по запросу пользователя."
)

# Output format fix: Content Factory уже умеет "weekly_plan" отдельно от
# "telegram" — свой system prompt и удвоенный max_output_tokens именно под
# многодневный/многонедельный план или несколько готовых материалов сразу
# (см. живой prod-баг: под "telegram" план на 2 недели не помещался в бюджет
# и подменялся фразой "если нужен план...").
#
# Живой prod-баг #2: паттерн изначально требовал ровно "план\w* на <ЧИСЛО>
# дней/недель" — «план публикаций на 14 дней» не совпадал (между "план" и
# "на" стоит ещё слово "публикаций"), а «контент-план на неделю» не совпадал
# вовсе (нет числа). Это тот же класс задачи (план/график публикаций на
# период), а не другая формулировка, поэтому паттерн обобщён: до 3 слов
# между "план/график/расписание" и "на", и период не обязан быть числовым
# ("на неделю"/"на месяц" тоже считаются).
#
# Второй класс той же проблемы: пользователь просит не план по дням, а явное
# КОЛИЧЕСТВО готовых материалов ("10 постов", "5 идей") или явную СЕРИЮ на
# период ("серия постов на месяц") — здесь тоже нужен бюджет на несколько
# полноценных материалов сразу, а не на один пост.
_CONTENT_PLAN_WITH_DURATION_PATTERN = re.compile(
    r"(?:план|график|расписание)\w*(?:\s+\w+){0,3}?\s+на\s+"
    r"(?:\d+\s*)?(?:дн\w*|недел\w*|месяц\w*)",
    re.IGNORECASE,
)
_CONTENT_SERIES_WITH_DURATION_PATTERN = re.compile(
    r"сери\w*(?:\s+\w+){0,3}?\s+на\s+(?:\d+\s*)?(?:дн\w*|недел\w*|месяц\w*)",
    re.IGNORECASE,
)
_MULTI_ITEM_QUANTITY_PATTERN = re.compile(
    r"\d+\s*(?:пост\w*|публикац\w*|материал\w*|иде\w*|сценар\w*|reels|рилс\w*|сторис\w*)",
    re.IGNORECASE,
)


def _wants_weekly_content_plan(task_text: str) -> bool:
    return bool(
        _CONTENT_PLAN_WITH_DURATION_PATTERN.search(task_text)
        or _CONTENT_SERIES_WITH_DURATION_PATTERN.search(task_text)
        or _MULTI_ITEM_QUANTITY_PATTERN.search(task_text)
    )


# Live prod bug: "Нужно переписать пост чтобы не обвинили в плагиате: <пост с
# ценами и процентами>" was executed under the same generic _FREE_TEXT_OBJECTIVE
# as any other free-text task ("выполнить задачу как техническое задание"),
# with no instruction distinguishing "rewrite this user-supplied text" from
# "analyze/verify these claims". Combined with the routing-level fix (see
# app/routing/safety.py HIGH_RISK_SAFETY_KEYWORDS + app/routing/router.py
# has_rewrite_action), this is the generation-time half: an explicit rewrite
# verb (same REWRITE_ACTION_KEYWORDS the router uses - one signal, not a
# second classifier) adds one extra constraint stating the task is TEXT
# TRANSFORMATION, not fact-checking, so the model does not hedge on or
# "verify" the user's own numbers, and does not default to a generic
# "проверьте/сверьте условия" tail when there is no real risk.
def _wants_rewrite(task_text: str) -> bool:
    lowered = task_text.lower()
    return any(kw in lowered for kw in REWRITE_ACTION_KEYWORDS)


_FREE_TEXT_REWRITE_CONSTRAINT = (
    "Команда пользователя — явный rewrite/paraphrase (перепиши, "
    "перефразируй, изложи другими словами, сделай уникальным, чтобы не было "
    "плагиата, сохрани смысл, но перепиши). Это задача TEXT TRANSFORMATION, "
    "а не факт-чек и не анализ достоверности. Текст в [UNTRUSTED SOURCE "
    "CONTENT - DATA] — исходный материал пользователя: сохрани все факты, "
    "цифры и смысл исходника без изменений, но измени формулировки, "
    "структуру и стиль. Не добавляй новые факты, которых нет в исходнике. "
    "Цены, проценты и другие цифры из исходника — это данные, "
    "предоставленные пользователем, а не новое коммерческое утверждение "
    "бота: не подвергай их сомнению, не проси перепроверить и не добавляй "
    "оговорки о проверке. Не завершай текст стандартными фразами вида "
    "«проверьте», «перепроверьте», «сверьте условия», если в самом исходнике "
    "нет реального существенного риска (гарантированный доход, опасное "
    "финансовое обещание, медицинский или юридический совет, явно опасное "
    "действие)."
)


_RADAR_OBJECTIVE = "Создать черновик информационного материала по выбранному Radar-сигналу."
_CONSTRAINTS = (
    "Черновик требует ручной проверки перед использованием.",
    # UX polish: живой тест Stage 3B1 показал типичные AI-хвосты в готовом
    # посте («если хотите, могу сравнить...») — они не читаются как текст
    # автора поста. Запрет на конкретные ассистентские фразы, а не на CTA
    # вообще: естественный призыв к действию (забронировать, написать,
    # перейти по ссылке) по-прежнему допустим.
    "Не завершай текст служебными фразами от имени ассистента любой длины и "
    "детализации: «если хотите, могу...», «могу помочь...», «напишите — "
    "разберу...», «могу сравнить варианты...», «могу помочь сравнить "
    "варианты по датам и погоде...» и аналогичными репликами AI, если "
    "пользователь явно не попросил такой CTA. Результат должен читаться как "
    "самостоятельный текст автора поста, а не как ответ ассистента. Обычный "
    "естественный для поста CTA (например, забронировать, написать в "
    "директ, перейти по ссылке) не запрещён.",
    # Quality fix: production показал утечку внутреннего мета-комментария о
    # надёжности источника прямо в готовый пост («Остальное в исходном
    # тексте — шутка и личная оценка, на них лучше не опираться.») — модель
    # рассуждала о том, каким фактам не стоит доверять и почему, ПРЯМО В
    # ТЕКСТЕ поста вместо того, чтобы просто не использовать их. Причина
    # исключения факта (безопасность/непроверенность/неуместность) должна
    # определять, что модель ПИШЕТ, а не появляться в самом посте как
    # рассуждение о процессе.
    "Никогда не пиши в тексте поста комментарии о надёжности, происхождении "
    "или уместности источника — например, «в исходном тексте», «остальное "
    "в источнике — ...», «на это лучше не опираться», «эту часть я не "
    "использовал, потому что...», «не удалось подтвердить». Если факт "
    "ненадёжен, неуместен или не подтверждён — просто не используй его в "
    "посте или сформулируй мысль без него, не объясняя читателю причину "
    "исключения. Готовый пост не должен содержать никаких следов твоих "
    "внутренних рассуждений о том, каким частям источника доверять.",
    # Stage 3B1: приоритет источников стиля — эти правила (безопасность,
    # бизнес-факты) всегда выше личного стиля пользователя. [PERSONAL STYLE
    # - DATA] влияет только на тон/формулировки и содержит avoid_phrases —
    # список слов/оборотов, которых явно нужно избегать. voice_sample
    # ("Мой стиль / Голос бренда") — тот же принцип, что и example_posts:
    # манера речи, не факты; короткая parenthetical-оговорка вместо
    # отдельного предложения — держит prefix компактным (см.
    # SOURCE_ANALYSIS_REQUEST_LIMIT/Content Factory 6000-символьный лимит).
    "Если задан раздел [PERSONAL STYLE - DATA], учитывай style_description, "
    "example_posts и voice_sample как ориентир тона и манеры речи, а "
    "avoid_phrases — как прямой запрет на эти слова/обороты в тексте. Это "
    "образцы манеры, а не факты: цены, даты, названия туров, акции, отели и "
    "другая конкретика из example_posts/voice_sample могут быть устаревшими "
    "и не считаются актуальной информацией. Личный стиль не должен "
    "противоречить бизнес-контексту, фактам источника и другим правилам выше.",
    "Если в [PERSONAL STYLE - DATA] заданы example_posts и/или voice_sample, "
    "они — более сильный ориентир манеры речи, чем общий тон бренда, если "
    "это не противоречит бизнес-контексту, фактам источника и правилам выше.",
)

# Fix: разбор присланной пользователем публикации («Разобрать публикацию»)
# слишком часто превращал живой кейс в стерильный текст — конкретные цифры и
# детали из disputed_claims либо вырезались целиком, либо единичный случай
# подавался как универсальное правило для всех. Три отдельных constraint'а
# вместо изменения общих _CONSTRAINTS (используются только этим flow —
# build_generation_spec, — чтобы не задеть обычную генерацию постов, Radar и
# client reply):
#   1) сначала свериться с уже доступным trusted-контекстом workspace, а не
#      сразу требовать «внешней проверки»;
#   2) факт, которого в trusted-контексте нет, — это не выдумка бота, а
#      конкретика присланного кейса: сохранить её с атрибуцией автору
#      источника, а не заменять обезличенной фразой без цифр;
#   3) не превращать конкретный случай в обещание результата для всех.
_SOURCE_CASE_ATTRIBUTION_CONSTRAINT = (
    "Утверждения из [SOURCE FACTS - DATA].disputed_claims — это конкретные "
    "детали присланного кейса, а не автоматически ложь и не повод для "
    "удаления. Сначала сверь каждое такое утверждение с [VERIFIED CLAIMS - "
    "ALLOWED FACTS] и [TRUSTED BUSINESS CONTEXT - DATA]: если там есть "
    "подтверждение — используй утверждение как обычный факт, без оговорок. "
    "Если [TRUSTED BUSINESS CONTEXT - DATA] или [VERIFIED CLAIMS - ALLOWED "
    "FACTS] прямо противоречат утверждению — явно отметь это противоречие в "
    "тексте, а не игнорируй его молча. Если ни один из этих разделов ничего "
    "не говорит по теме утверждения — сохрани утверждение как конкретный "
    "факт ИЗ ПРИСЛАННОЙ ПУБЛИКАЦИИ с явной атрибуцией автору источника "
    "(например, «по словам автора», «в этом конкретном бронировании», «по "
    "данным автора публикации», «на момент сравнения»), а не удаляй его и не "
    "заменяй общей фразой без цифр и деталей. Конкретные числа, даты, "
    "названия и суммы из disputed_claims можно и нужно сохранять как "
    "атрибутированные данные конкретного случая."
)
_SOURCE_CASE_SCOPE_CONSTRAINT = (
    "Не превращай единичный случай из источника в универсальное обещание "
    "результата для всех читателей (например, «в этом кейсе получилось "
    "около 67%» нельзя переписывать как «мы всегда даём скидку 67%»). Если "
    "исходный текст описывает один конкретный случай, прямо покажи, что это "
    "пример, а не гарантия: добавь короткую оговорку вида «это не значит, "
    "что результат будет таким в каждом бронировании» рядом с фактом, а не "
    "вместо него. Оговорка не должна вытеснять уже сохранённую конкретику "
    "случая (цифры, даты, детали)."
)
_SOURCE_ANALYSIS_CONSTRAINTS = _CONSTRAINTS + (
    _SOURCE_CASE_ATTRIBUTION_CONSTRAINT,
    _SOURCE_CASE_SCOPE_CONSTRAINT,
)

# Fix: перед тем как отправить disputed_claims модели как «требует проверки»,
# сверяем их с уже доступным trusted Business/Knowledge Context workspace
# (verified claims профиля) — тем же источником, что и verified_claims_allowed
# ниже. Подтверждённое контекстом утверждение перестаёт быть спорным и
# переходит в verified_claims_allowed; остальное остаётся в disputed_claims и
# помечается по правилам _SOURCE_CASE_ATTRIBUTION_CONSTRAINT выше, а не сразу
# как «требует внешней проверки».
#
# Review fix: первая версия сверки считала claim подтверждённым по
# пересечению стеммированных слов (bag-of-words overlap). Проверка на
# adversarial-примерах показала, что это небезопасно — overlap не видит
# смысла, только буквы: «можно» и «нельзя» пересекаются по всем остальным
# словам предложения и совпадение считалось подтверждением своей же
# противоположности; то же самое с разными числами (10% vs 47%) и разными
# тарифами («Стандарт» vs «Премиум») — оба случая ошибочно промоутились в
# verified. Одновременно тот же overlap иногда НЕ находил реально
# подтверждённый claim, если у verified-claim было немного больше слов, чем
# порог позволял. Underlying проблема не лечится точечными патчами overlap
# (ещё эвристика поверх эвристики) — precision здесь важнее recall: лучше
# оставить связанный claim disputed (и он всё равно попадёт в generation
# request с attribution — см. _SOURCE_CASE_ATTRIBUTION_CONSTRAINT), чем
# ошибочно объявить verified утверждение, которое источник не подтверждает
# или прямо опровергает.
#
# Поэтому promotion теперь строго детерминированный: только точное
# совпадение текста claim после БЕЗОПАСНОЙ нормализации (регистр, пунктуация,
# пробелы). Числа, отрицания («не», «нельзя»), модальные слова («можно»,
# «нужно»), названия тарифов и любые другие смысловые токены нормализация не
# трогает — значит "можно" и "нельзя", "10%" и "47%", "Стандарт" и "Премиум"
# всегда дают разные нормализованные строки и никогда не совпадут случайно.
_CLAIM_NORMALIZE_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_CLAIM_NORMALIZE_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)


def _normalize_claim_text(text: str) -> str:
    lowered = text.strip().lower()
    without_punctuation = _CLAIM_NORMALIZE_PUNCTUATION_RE.sub(" ", lowered)
    return _CLAIM_NORMALIZE_WHITESPACE_RE.sub(" ", without_punctuation).strip()


def _confirmed_by_trusted_claim(disputed_claim: str, verified_claim_text: str) -> bool:
    normalized_disputed = _normalize_claim_text(disputed_claim)
    normalized_verified = _normalize_claim_text(verified_claim_text)
    if not normalized_disputed or not normalized_verified:
        return False
    return normalized_disputed == normalized_verified


def _reconcile_disputed_claims_with_trusted_context(
    disputed_claims: tuple[str, ...],
    verified_claims: tuple[Mapping[str, Any], ...],
) -> tuple[tuple[str, ...], tuple[Mapping[str, Any], ...]]:
    """Проблема 4: сначала проверяем trusted Business/Knowledge Context, и
    только если он ничего не знает — оставляем факт непроверенным.

    Возвращает (оставшиеся disputed_claims, дополнительные verified claims,
    подтверждённые контекстом).
    """
    remaining: list[str] = []
    promoted: list[Mapping[str, Any]] = []
    for claim in disputed_claims:
        match = next(
            (
                verified for verified in verified_claims
                if _confirmed_by_trusted_claim(claim, verified.get("text", ""))
            ),
            None,
        )
        if match is None:
            remaining.append(claim)
            continue
        promoted.append({
            "text": claim,
            "verification_status": "verified",
            "evidence_reference": match.get("evidence_reference") or "business_profile",
        })
    return tuple(remaining), tuple(promoted)


# Free-text task fulfillment fix (та же категория багов, что и _FREE_TEXT_OBJECTIVE
# выше, только для двух конкретных живых кейсов):
#
# 1. Пользователь просит явное КОЛИЧЕСТВО материалов («10 постов», «5 идей»)
#    — живой тест показал, что модель делает один пример и предлагает
#    подготовить остальные отдельно вместо того, чтобы сразу выдать все N.
# 2. Пользователь не называет тему явно («серия постов на месяц»), но
#    запрос идёт из workspace с заполненным Business Profile — живой тест
#    показал отказ и просьбу прислать тему/аудиторию/тезисы/источники,
#    хотя эти данные уже есть в [TRUSTED BUSINESS CONTEXT - DATA].
#
# Оба правила — про КЛАСС задачи (любое число, любой пропущенный topic), а
# не про конкретную формулировку теста.
_FREE_TEXT_QUANTITY_CONSTRAINT = (
    "Если в задаче явно указано количество единиц контента (например, «10 "
    "постов», «5 идей», «3 сценария»), результат должен содержать ровно "
    "это количество полностью готовых, самостоятельных единиц прямо в этом "
    "ответе. Запрещено делать один пример и предлагать подготовить "
    "остальные отдельно («вот пример поста, могу подготовить остальные 9 в "
    "таком же стиле») — каждая единица должна быть в тексте ответа."
)
_FREE_TEXT_TOPIC_FALLBACK_CONSTRAINT = (
    "Если в задаче не указана явная тема, аудитория или тезисы, но задан "
    "раздел [TRUSTED BUSINESS CONTEXT - DATA] с данными о бизнесе "
    "(business_name, short_description, specializations и т.п.), используй "
    "этот контекст как тему по умолчанию и выполни задачу сразу, не спрашивая "
    "пользователя. Отказ от выполнения и просьба прислать тему/аудиторию/"
    "тезисы/источники допустимы только если [TRUSTED BUSINESS CONTEXT - DATA] "
    "пуст и из самой задачи тему определить невозможно."
)
_FREE_TEXT_CONSTRAINTS = _CONSTRAINTS + (
    _FREE_TEXT_QUANTITY_CONSTRAINT,
    _FREE_TEXT_TOPIC_FALLBACK_CONSTRAINT,
)

_CLIENT_REPLY_OBJECTIVE = "Сформировать короткий личный ответ клиенту в Telegram по его вопросу."

# Stage 3B1: TRAVEL_ASSISTANT (client reply) больше не обходит structured
# orchestration через сырой source_text — та же логика приоритета источников
# стиля, что и в _CONSTRAINTS выше, плюс сохранённая формулировка прежнего
# ручного prompt'а из app/handlers/tasks.py (_draft_request_for).
_CLIENT_REPLY_CONSTRAINTS = (
    "Черновик требует ручной проверки перед отправкой.",
    "Ответь простыми словами и по существу. Не обещай доход, окупаемость или "
    "гарантированные скидки. Не утверждай, что формат подходит всем. Не "
    "используй фразу «без давления». Если точных данных недостаточно, не "
    "выдумывай: предложи уточнить детали или спокойно разобрать вопрос лично.",
    # UX polish: живой тест Stage 3B1 показал типичный AI-хвост в ответе
    # клиенту («если хотите, можно сравнить... напишите — разберу»). Это
    # сообщение живого человека клиенту, а не реплика ассистента — обычное
    # предложение следующего шага уместно, но не в виде универсального
    # AI-хвоста.
    "Не завершай ответ служебными фразами от имени ассистента: «если "
    "хотите, могу...», «могу помочь...», «напишите — разберу...», «могу "
    "сравнить варианты...» и аналогичными репликами AI. Это сообщение "
    "живого человека клиенту, а не ответ ассистента: естественное "
    "предложение следующего шага уместно (например, «уточню детали и "
    "напишу точнее» или «скажите даты — посмотрю варианты»), но оно должно "
    "звучать как реплика самого пользователя, а не как универсальный "
    "AI-хвост.",
    "Если задан раздел [PERSONAL STYLE - DATA], учитывай style_description, "
    "example_posts и voice_sample как ориентир тона и манеры речи, а "
    "avoid_phrases — как прямой запрет на эти слова/обороты в тексте. Это "
    "образцы манеры, а не факты: цены, даты, названия туров, акции, отели и "
    "другая конкретика из example_posts/voice_sample могут быть устаревшими "
    "и не считаются актуальной информацией для ответа клиенту. Личный стиль "
    "не должен противоречить бизнес-контексту, verified/unverified claims и "
    "другим правилам выше.",
    "Если в [PERSONAL STYLE - DATA] заданы example_posts и/или voice_sample, "
    "они — более сильный ориентир манеры речи, чем общий тон бренда, если "
    "это не противоречит бизнес-контексту, verified/unverified claims и "
    "правилам выше.",
)

# Добавляется к _CLIENT_REPLY_CONSTRAINTS только когда decision.safety_level
# не NOT_REQUIRED — прежнее поведение safety_instruction в _draft_request_for.
_CLIENT_REPLY_SAFETY_CONSTRAINT = (
    "Это вопрос с обязательной Safety-проверкой. Не сообщай цены, тарифы, "
    "доступность, способы оплаты, варианты бронирования или сравнения как "
    "установленный факт. Дай только общее объяснение и прямо укажи, что "
    "конкретные условия нужно сверить вручную."
)

_INFORMATIONAL_OBJECTIVE = (
    "Ответить пользователю по существу и по фактам на его собственный "
    "вопрос о путешествиях. Это прямой ответ travel-ассистента самому "
    "пользователю, а НЕ черновик сообщения для третьего лица (клиента "
    "партнёра) — не формулируй ответ как «клиенту можно ответить...» или "
    "«сообщите клиенту...»."
)

# Live prod bug: тот же ORCHESTRAVEL Web Search сценарий ("Какие сейчас
# изменения правил въезда в Индонезию для россиян?") получал корректный
# WebSearchService-контекст, но build_client_reply_generation_spec's
# OBJECTIVE ("Сформировать короткий личный ответ клиенту...") заставлял
# модель писать так, будто ответ адресован третьему лицу — заголовок
# "Черновик ответа клиенту" был симптомом, а не причиной: сам prompt
# инструктировал модель как для client-reply, даже когда вопрос задал сам
# пользователь напрямую. Эти constraints — тот же набор правил, что и
# _CLIENT_REPLY_CONSTRAINTS (facts-first, без обещаний дохода, без
# ассистентских AI-хвостов, приоритет PERSONAL STYLE), только без единого
# упоминания «клиента» — исправление именно prompt'а, а не только
# Telegram-заголовка поверх него.
_INFORMATIONAL_CONSTRAINTS = (
    "Ответ обращён напрямую к пользователю, который задал вопрос — это не "
    "черновик реплики клиенту и не инструкция, что сказать клиенту. Не "
    "используй обороты «клиенту можно ответить», «вы можете сказать "
    "клиенту», «сообщите клиенту» и подобные: отвечай самому пользователю "
    "от первого лица ассистента, по существу вопроса.",
    "Опирайся на [SOURCE FACTS - DATA] и [VERIFIED CLAIMS - ALLOWED FACTS], "
    "если они заданы. Если точных и актуальных данных недостаточно, прямо "
    "скажи об этом и посоветуй проверить официальный источник — не "
    "выдумывай факты, цифры и правила.",
    "Не обещай доход, окупаемость или гарантированные скидки. Не утверждай, "
    "что формат подходит всем.",
    "Не завершай ответ служебными фразами от имени ассистента: «если "
    "хотите, могу...», «могу помочь...», «напишите — разберу...», «могу "
    "сравнить варианты...» и аналогичными репликами AI.",
    "Если задан раздел [PERSONAL STYLE - DATA], учитывай style_description, "
    "example_posts и voice_sample как ориентир тона и манеры речи, а "
    "avoid_phrases — как прямой запрет на эти слова/обороты в тексте. Это "
    "образцы манеры, а не факты: цены, даты, названия туров, акции, отели и "
    "другая конкретика из example_posts/voice_sample могут быть устаревшими "
    "и не считаются актуальной информацией для ответа.",
    "Если в [PERSONAL STYLE - DATA] заданы example_posts и/или voice_sample, "
    "они — более сильный ориентир манеры речи, чем общий тон бренда, если "
    "это не противоречит фактам и другим правилам выше.",
)

# Тот же текст, что и _CLIENT_REPLY_SAFETY_CONSTRAINT — он уже не упоминает
# «клиента», поэтому переиспользуется как есть, без отдельного дубликата.
_INFORMATIONAL_SAFETY_CONSTRAINT = _CLIENT_REPLY_SAFETY_CONSTRAINT

# Тот же базовый constraint + правила стиля именно для Radar-черновика: черновик
# уходит пользователю как самостоятельный готовый пост, а не как ответ
# ассистента, поэтому внутренние пометки о проверке и ассистентские концовки
# в нём недопустимы. Не переиспользуется другими flow — только build_radar_generation_spec.
_RADAR_CONSTRAINTS = (
    "Черновик требует ручной проверки перед использованием.",
    "Результат — самостоятельный готовый пост для соцсети, а не ответ ассистента пользователю.",
    # Radar UX / Content Quality: живой тест показал, что Radar-черновики
    # часто получаются энциклопедическими — общая статья «на тему» вместо
    # текста про конкретный сигнал из [SOURCE FACTS - DATA].
    "Начни пост с конкретной зацепки (hook) по первому предложению — она "
    "должна отражать именно title/summary конкретного сигнала из [SOURCE "
    "FACTS - DATA], а не быть общим вступлением на тему.",
    "Пиши именно про этот сигнал — конкретный повод, событие или наблюдение "
    "из [SOURCE FACTS - DATA]. Не превращай пост в общую обзорную статью по "
    "теме шире, чем сам сигнал.",
    "Не включай в текст поста внутренние заметки о процессе: «нужно проверить», "
    "«лучше перепроверить», «по исходному посту», «в исходном тексте», "
    "«на это лучше не опираться», «личная оценка», «не удалось подтвердить» "
    "и подобные формулировки — ни в начале, ни в середине, ни в конце поста.",
    "Если конкретный факт из источника не подтверждён, не уместен или "
    "выглядит как шутка/личное мнение автора, а не факт — просто не "
    "используй его или сформулируй мысль без него. Не объясняй читателю, "
    "какую часть источника ты исключил и почему: причина исключения "
    "определяет, что ты пишешь, а не появляется в самом посте как "
    "рассуждение о процессе.",
    "Не заканчивай пост фразами от имени ассистента любой длины: "
    "«могу...», «если хотите...», «могу помочь...», «могу помочь сравнить "
    "варианты по датам и погоде...». Если в посте есть призыв к действию, "
    "он должен быть органичной частью текста, а не отдельным предложением "
    "от ассистента.",
    "Не придумывай факты, которых нет в источнике или в бизнес-контексте.",
    "Утверждения, перечисленные в [SOURCE FACTS - DATA].disputed_claims, не "
    "подтверждены — не подавай их как факт. Если нет уверенности, что "
    "утверждение верно, не включай его в текст вообще, а не проси читателя "
    "проверить это самому.",
    "Если задан раздел [PERSONAL STYLE - DATA], учитывай style_description, "
    "example_posts и voice_sample как ориентир тона и манеры речи, а "
    "avoid_phrases — как прямой запрет на эти слова/обороты в тексте. Это "
    "образцы манеры, а не факты: цены, даты, названия туров, акции, отели и "
    "другая конкретика из example_posts/voice_sample могут быть устаревшими "
    "и не считаются актуальной информацией для поста. Личный стиль не "
    "должен противоречить бизнес-контексту, фактам источника и другим "
    "правилам выше.",
    "Если в [PERSONAL STYLE - DATA] заданы example_posts и/или voice_sample, "
    "они — более сильный ориентир манеры речи, чем нейтральный обзорный "
    "тон, если это не противоречит бизнес-контексту, фактам источника и "
    "правилам выше.",
    # Quality fix: живой пример показал пост, который ОБСУЖДАЕТ сигнал вместо
    # того, чтобы БЫТЬ готовой публикацией («в этом сигнале цепляет...»,
    # «источник сообщает...», «в тексте упоминается...») — это отдельная от
    # уже запрещённых выше мета-фраз о надёжности проблема: модель
    # рассказывает читателю про сам сигнал/источник как объект анализа, а не
    # пишет пост для его аудитории.
    "Пиши готовый пост для читателя, а не разбор или анализ сигнала. Не "
    "используй обороты, которые описывают сам сигнал/источник как объект "
    "рассмотрения: «в этом сигнале цепляет...», «источник сообщает...», "
    "«в тексте упоминается...», «как видно из этого сообщения...», «автор "
    "пишет, что...». Пиши как автор поста от своего лица, используя факты "
    "из источника, а не рассказывай читателю о существовании источника.",
    # Quality fix: тот же пример — заголовок поста был обрезанным исходным
    # title сигнала (Telegram-заголовки часто обрываются на многоточии или
    # на середине фразы). title — черновой повод, не готовый заголовок.
    "Не копируй title сигнала дословно как заголовок или первую строку "
    "поста, особенно если он обрывается на полуслове или многоточии — "
    "сформулируй свой собственный конкретный хук по смыслу title/summary.",
    # Quality fix: тот же пример без необходимости называл источник
    # (Tripster) в тексте поста, хотя пост не был ни о самом источнике, ни
    # сравнением с ним.
    "Не упоминай в тексте поста название source_name/конкурента из [SOURCE "
    "FACTS - DATA], если пост не является прямым сравнением с этим "
    "источником или сравнение не входит в задачу — используй факты и повод "
    "из сигнала, не рекламируя и не называя источник, из которого он взят.",
)

# Fix: «Идеи для постов» → «Создать материал» из Competitor Intelligence
# раньше собирал произвольный task_text и прогонял его через
# build_free_text_generation_spec (_FREE_TEXT_OBJECTIVE) — objective для
# ЛЮБОЙ пользовательской задачи, без единого слова про то, что нужно
# сделать именно с конкурентным сигналом. Живой prod-кейс показал типичный
# результат такого разрыва: сигнал «Trip.com обновляет промокоды каждую
# неделю» на выходе превращался в общий совет «проверяйте срок акции,
# условия, направление и даты» — текст без наблюдения и без тенденции,
# который можно написать и без Competitor Intelligence вообще. Ценность
# самого сигнала терялась ещё на этапе postановки задачи модели.
#
# Тот же паттерн, что и Radar (build_radar_generation_spec) — внешний
# сигнал как повод для контента, а не тема для пересказа, — но с другим
# требованием: не просто хук по первому предложению, а явная авторская
# мысль о тренде/изменении поведения рынка, которую сигнал иллюстрирует.
_COMPETITOR_SIGNAL_OBJECTIVE = (
    "Написать авторский пост о travel-рынке, поводом для которого стал "
    "наблюдаемый сигнал конкурента из [SOURCE FACTS - DATA]. Сигнал — это "
    "повод для самостоятельной мысли о тенденции или изменении поведения на "
    "рынке, а не тема для пересказа или рекламы конкурента."
)
_COMPETITOR_SIGNAL_INSIGHT_CONSTRAINT = (
    "В тексте должно быть явно понятно три вещи: (1) какой конкретно сигнал "
    "замечен — по [SOURCE FACTS - DATA].key_thesis и .competitor_signal; "
    "(2) какую тенденцию или изменение поведения travel-рынка/путешественника "
    "этот сигнал показывает; (3) почему это важно путешественнику или "
    "партнёру Travel Advantage. Если хотя бы один из трёх пунктов не читается "
    "в тексте явно, черновик не выполнил задачу."
)
_COMPETITOR_SIGNAL_NO_GENERIC_ADVICE_CONSTRAINT = (
    "Не превращай сигнал в общий совет без анализа рынка (например, "
    "«проверяйте срок акции, условия, направление и даты»). Такой текст не "
    "показывает ни наблюдение, ни тенденцию и мог быть написан без этого "
    "конкретного сигнала — он не может быть готовым результатом. "
    "Самостоятельная аналитическая мысль о рынке обязательна, а не "
    "напоминание проверить детали."
)
_COMPETITOR_SIGNAL_NO_RECAP_CONSTRAINT = (
    "Не пересказывай сигнал конкурента как новость и не рекламируй "
    "конкурента: не описывай его предложение как выгодное для читателя и не "
    "пиши текст так, будто это анонс от лица конкурента. "
    "[SOURCE FACTS - DATA].source_title и .source_url — только внутренняя "
    "атрибуция происхождения сигнала, а не тема поста."
)
_COMPETITOR_SIGNAL_TA_LINK_CONSTRAINT = (
    "Связь с Travel Advantage допустима только через факты из [VERIFIED "
    "CLAIMS - ALLOWED FACTS]. Если этот раздел пуст или не содержит факта, "
    "относящегося к теме поста, не упоминай Travel Advantage вообще и не "
    "придумывай сравнение или преимущество перед конкурентом."
)
_COMPETITOR_SIGNAL_NO_ADVERTISING_STYLE_CONSTRAINT = (
    "Не используй шаблонный рекламный стиль: превосходная степень («лучший», "
    "«уникальный»), искусственное давление срочности («успей», «только "
    "сегодня») и прямые призывы воспользоваться предложением конкурента "
    "запрещены. Пиши как автор, формирующий собственное мнение о рынке, а "
    "не как копирайтер чужой акции."
)
_COMPETITOR_SIGNAL_CONSTRAINTS = _CONSTRAINTS + (
    _COMPETITOR_SIGNAL_INSIGHT_CONSTRAINT,
    _COMPETITOR_SIGNAL_NO_GENERIC_ADVICE_CONSTRAINT,
    _COMPETITOR_SIGNAL_NO_RECAP_CONSTRAINT,
    _COMPETITOR_SIGNAL_TA_LINK_CONSTRAINT,
    _COMPETITOR_SIGNAL_NO_ADVERTISING_STYLE_CONSTRAINT,
)


class MaterialOrchestrationService:
    """Build a provider-neutral spec from inputs authorized by the caller."""

    def build_generation_spec(
        self,
        workspace_id: int,
        source: Source,
        analysis: SourceAnalysis,
        profile: BusinessProfile | None,
        *,
        artifact_type: str,
        output_format: str,
        user_preferences: WorkspaceUserPreferences | None = None,
    ) -> GenerationSpec:
        if source.workspace_id != workspace_id or analysis.workspace_id != workspace_id:
            raise PermissionError("Source и SourceAnalysis не принадлежат workspace")
        if analysis.source_id != source.id:
            raise ValueError("SourceAnalysis не соответствует Source")
        trusted_context, tone_preferences, verified, unverified, revision = (
            _profile_generation_values(workspace_id, profile)
        )
        # Проблема 4: сначала сверяем disputed_claims источника с уже
        # доступным trusted-контекстом workspace, а не сразу помечаем их как
        # требующие внешней проверки.
        remaining_disputed, promoted_verified = (
            _reconcile_disputed_claims_with_trusted_context(
                analysis.disputed_claims, verified,
            )
        )

        return GenerationSpec(
            action_type=GenerationAction.CREATE_ARTIFACT,
            artifact_type=artifact_type,
            objective=_OBJECTIVE,
            audience=tuple(dict.fromkeys(
                [*trusted_context.get("audiences", ()), *analysis.target_audiences]
            )),
            output_format=output_format,
            source_facts={
                "summary": analysis.summary,
                "key_facts": analysis.key_facts,
                "audience_value": analysis.audience_value,
                "content_angles": analysis.content_angles,
                "recommended_formats": analysis.recommended_formats,
                "disputed_claims": remaining_disputed,
                "warnings": analysis.warnings,
            },
            trusted_business_context=trusted_context,
            untrusted_source_content=source.original_text or "",
            tone_preferences=tone_preferences,
            personal_style=_personal_style_values(user_preferences),
            verified_claims_allowed=verified + promoted_verified,
            unverified_claims_requiring_caution=tuple(unverified),
            constraints=_SOURCE_ANALYSIS_CONSTRAINTS,
            profile_revision_used=revision,
        )

    def build_free_text_generation_spec(
        self,
        workspace_id: int,
        task_text: str,
        profile: BusinessProfile | None,
        *,
        user_preferences: WorkspaceUserPreferences | None = None,
    ) -> GenerationSpec:
        if not isinstance(task_text, str) or not task_text.strip():
            raise ValueError("task_text не должен быть пустым")
        trusted_context, tone_preferences, verified, unverified, revision = (
            _profile_generation_values(workspace_id, profile)
        )
        output_format = (
            "weekly_plan" if _wants_weekly_content_plan(task_text) else "telegram"
        )
        constraints = _FREE_TEXT_CONSTRAINTS
        if _wants_rewrite(task_text):
            constraints = (*constraints, _FREE_TEXT_REWRITE_CONSTRAINT)
        return GenerationSpec(
            action_type=GenerationAction.CREATE_ARTIFACT,
            artifact_type="post",
            objective=_FREE_TEXT_OBJECTIVE,
            audience=tuple(trusted_context.get("audiences", ())),
            output_format=output_format,
            source_facts={},
            trusted_business_context=trusted_context,
            untrusted_source_content=task_text,
            tone_preferences=tone_preferences,
            personal_style=_personal_style_values(user_preferences),
            verified_claims_allowed=verified,
            unverified_claims_requiring_caution=unverified,
            constraints=constraints,
            profile_revision_used=revision,
        )

    def build_competitor_signal_generation_spec(
        self,
        workspace_id: int,
        profile: BusinessProfile | None,
        *,
        competitor_signal: str,
        key_thesis: str,
        own_post_angle: str,
        audience_value: str,
        source_title: str,
        source_url: str,
        travel_advantage_link: str | None,
        user_preferences: WorkspaceUserPreferences | None = None,
        # "Что можно сделать" (Competitor Intelligence -> материал): тот же
        # builder, что и раньше, просто параметризованный по artifact_type -
        # не второй генератор. "post" (по умолчанию, обратная совместимость с
        # существующим Telegram-вызовом) или "client_message" - оба уже
        # понятны Content Factory через _PROVIDER_MATERIAL_TYPES ниже.
        artifact_type: str = "post",
    ) -> GenerationSpec:
        trusted_context, tone_preferences, verified, unverified, revision = (
            _profile_generation_values(workspace_id, profile)
        )
        # travel_advantage_link уже отфильтрован источником (Competitor
        # Intelligence строит его только из verified KB, см.
        # app/services/competitor_intelligence.py) — здесь он идёт в
        # VERIFIED CLAIMS, а не в SOURCE FACTS, чтобы modель не могла
        # трактовать его как ещё один произвольный факт источника и не
        # добавляла к нему собственные сравнения с конкурентом.
        if travel_advantage_link:
            verified = (*verified, {
                "text": travel_advantage_link,
                "verification_status": "verified",
                "evidence_reference": "verified_ta_knowledge",
            })
        return GenerationSpec(
            action_type=GenerationAction.CREATE_ARTIFACT,
            artifact_type=artifact_type,
            objective=_COMPETITOR_SIGNAL_OBJECTIVE,
            audience=tuple(trusted_context.get("audiences", ())),
            output_format="telegram",
            source_facts={
                "competitor_signal": competitor_signal,
                "key_thesis": key_thesis,
                "own_post_angle": own_post_angle,
                "audience_value": audience_value,
                "source_title": source_title,
                "source_url": source_url,
            },
            trusted_business_context=trusted_context,
            untrusted_source_content=key_thesis,
            tone_preferences=tone_preferences,
            personal_style=_personal_style_values(user_preferences),
            verified_claims_allowed=verified,
            unverified_claims_requiring_caution=unverified,
            constraints=_COMPETITOR_SIGNAL_CONSTRAINTS,
            profile_revision_used=revision,
        )

    def build_radar_generation_spec(
        self,
        workspace_id: int,
        profile: BusinessProfile | None,
        *,
        title: str,
        summary: str,
        source_type: str,
        origin_type: str,
        url: str,
        category: str,
        reason: str,
        analysis: SourceAnalysisPayload | None = None,
        user_preferences: WorkspaceUserPreferences | None = None,
        # Signal -> материал (Radar): тот же builder, параметризованный по
        # artifact_type - "post" (по умолчанию, обратная совместимость с
        # существующим Telegram-вызовом) или "client_message".
        artifact_type: str = "post",
    ) -> GenerationSpec:
        trusted_context, tone_preferences, verified, unverified, revision = (
            _profile_generation_values(workspace_id, profile)
        )
        # Тот же Source Analysis, что и в обычном Content Factory flow
        # (llm_provider.analyze_source): disputed_claims/warnings идут в
        # source_facts как DATA, а не как отдельный parallel-механизм анализа.
        # analysis отсутствует (анализ недоступен/не выполнен) — fail-safe:
        # просто нет структурированного списка спорных утверждений, а не
        # пустые списки выдаются за "ничего спорного не найдено".
        disputed_claims = analysis.disputed_claims if analysis is not None else ()
        analysis_warnings = analysis.warnings if analysis is not None else ()
        return GenerationSpec(
            action_type=GenerationAction.CREATE_ARTIFACT,
            artifact_type=artifact_type,
            objective=_RADAR_OBJECTIVE,
            audience=tuple(trusted_context.get("audiences", ())),
            output_format="telegram",
            source_facts={
                "title": title,
                "summary": summary,
                "source_type": source_type,
                "origin_type": origin_type,
                "url": url,
                "category": category,
                "reason": reason,
                "disputed_claims": disputed_claims,
                "warnings": analysis_warnings,
            },
            trusted_business_context=trusted_context,
            untrusted_source_content="\n".join(
                value for value in (title, summary) if value
            ),
            tone_preferences=tone_preferences,
            personal_style=_personal_style_values(user_preferences),
            verified_claims_allowed=verified,
            unverified_claims_requiring_caution=unverified,
            constraints=_RADAR_CONSTRAINTS,
            profile_revision_used=revision,
        )

    def build_client_reply_generation_spec(
        self,
        workspace_id: int,
        client_question: str,
        profile: BusinessProfile | None,
        *,
        safety_required: bool,
        user_preferences: WorkspaceUserPreferences | None = None,
        knowledge_bundle: KnowledgeBundle | None = None,
    ) -> GenerationSpec:
        """Stage 3B1: заменяет прежний прямой вызов provider.generate_draft()
        для TRAVEL_ASSISTANT (client reply) в app/handlers/tasks.py — тот путь
        полностью обходил structured orchestration и personal_style. Здесь
        используется тот же BusinessProfile workspace, verified/unverified
        claims и приоритет источников стиля, что и в остальных build_*
        методах этого сервиса.
        """
        if not isinstance(client_question, str) or not client_question.strip():
            raise ValueError("client_question не должен быть пустым")
        trusted_context, tone_preferences, verified, unverified, revision = (
            _profile_generation_values(workspace_id, profile)
        )
        constraints = _CLIENT_REPLY_CONSTRAINTS
        if safety_required:
            constraints = (*constraints, _CLIENT_REPLY_SAFETY_CONSTRAINT)
        source_facts: dict[str, Any] = {}
        if knowledge_bundle is not None:
            knowledge = build_knowledge_generation_context(knowledge_bundle)
            source_facts.update(knowledge.source_facts)
            verified = (*verified, *knowledge.verified_claims)
            constraints = (*constraints, *knowledge.constraints)
        return GenerationSpec(
            action_type=GenerationAction.CREATE_ARTIFACT,
            artifact_type="client_message",
            objective=_CLIENT_REPLY_OBJECTIVE,
            audience=tuple(trusted_context.get("audiences", ())),
            output_format="telegram",
            source_facts=source_facts,
            trusted_business_context=trusted_context,
            untrusted_source_content=client_question,
            tone_preferences=tone_preferences,
            personal_style=_personal_style_values(user_preferences),
            verified_claims_allowed=verified,
            unverified_claims_requiring_caution=unverified,
            constraints=constraints,
            profile_revision_used=revision,
        )

    def build_informational_generation_spec(
        self,
        workspace_id: int,
        question: str,
        profile: BusinessProfile | None,
        *,
        safety_required: bool,
        user_preferences: WorkspaceUserPreferences | None = None,
        knowledge_bundle: KnowledgeBundle | None = None,
    ) -> GenerationSpec:
        """TRAVEL_ASSISTANT (informational): a factual/current travel
        question the user asked for themselves (e.g. "Какие сейчас изменения
        правил въезда в Индонезию для россиян?") - NOT a "what do I tell my
        client" request (see build_client_reply_generation_spec for that,
        used when app.handlers.tasks detects an explicit client-intent
        phrase). artifact_type stays "client_message" (Content Factory's
        already-whitelisted "client_question" material type is the closest
        Q&A-shaped fit) - only OBJECTIVE/CONSTRAINTS change, which is what
        actually drives the client-reply framing: build_radar_generation_spec
        and build_competitor_signal_generation_spec above already reuse the
        SAME "post" material type for completely different personas each, so
        material_type is Content Factory's internal category label, not a
        per-call system-prompt override.
        """
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question не должен быть пустым")
        trusted_context, tone_preferences, verified, unverified, revision = (
            _profile_generation_values(workspace_id, profile)
        )
        constraints = _INFORMATIONAL_CONSTRAINTS
        if safety_required:
            constraints = (*constraints, _INFORMATIONAL_SAFETY_CONSTRAINT)
        source_facts: dict[str, Any] = {}
        if knowledge_bundle is not None:
            knowledge = build_knowledge_generation_context(knowledge_bundle)
            source_facts.update(knowledge.source_facts)
            verified = (*verified, *knowledge.verified_claims)
            constraints = (*constraints, *knowledge.constraints)
        return GenerationSpec(
            action_type=GenerationAction.CREATE_ARTIFACT,
            artifact_type="client_message",
            objective=_INFORMATIONAL_OBJECTIVE,
            audience=tuple(trusted_context.get("audiences", ())),
            output_format="telegram",
            source_facts=source_facts,
            trusted_business_context=trusted_context,
            untrusted_source_content=question,
            tone_preferences=tone_preferences,
            personal_style=_personal_style_values(user_preferences),
            verified_claims_allowed=verified,
            unverified_claims_requiring_caution=unverified,
            constraints=constraints,
            profile_revision_used=revision,
        )


def _personal_style_values(
    user_preferences: WorkspaceUserPreferences | None,
) -> dict[str, Any]:
    """Личный стиль КОНКРЕТНОГО пользователя — отдельная DATA-секция от
    trusted_business_context (стиль компании). Пусто, если пользователь
    ничего не заполнил — тогда генерация просто не получает эту секцию,
    не заменяет её выдуманными значениями.
    """
    if user_preferences is None:
        return {}
    values: dict[str, Any] = {}
    if user_preferences.style_description.strip():
        values["style_description"] = user_preferences.style_description
    if user_preferences.example_posts:
        values["example_posts"] = list(user_preferences.example_posts)
    if user_preferences.avoid_phrases:
        values["avoid_phrases"] = list(user_preferences.avoid_phrases)
    # "Мой стиль / Голос бренда": один цельный вставленный образец текста -
    # тот же приоритет и то же "это манера, не факты" правило, что и
    # example_posts (см. constraint-тексты ниже), просто отдельное поле,
    # т.к. UX для него - одна большая textarea, а не список из нескольких
    # примеров, добавляемых по одному.
    if user_preferences.voice_sample.strip():
        values["voice_sample"] = user_preferences.voice_sample
    return values


def _profile_generation_values(
    workspace_id: int, profile: BusinessProfile | None,
) -> tuple[
    dict[str, Any], dict[str, Any], tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...], int | None,
]:
    if profile is not None and profile.workspace_id != workspace_id:
        raise PermissionError("Business Profile не принадлежит workspace")
    if profile is not None and profile.profile_status not in {"usable", "incomplete"}:
        raise ValueError("Неизвестный status Business Profile")

    trusted_context: dict[str, Any] = {}
    tone_preferences: dict[str, Any] = {}
    verified: list[Mapping[str, Any]] = []
    unverified: list[Mapping[str, Any]] = []
    revision = None
    if profile is not None:
        projection = (
            build_content_context(profile)
            if profile.profile_status == "usable"
            else build_limited_content_context(profile)
        )
        trusted_context = dict(projection)
        trusted_context.pop("claims", None)
        # Standard generation does not need direct contact details. Explicit
        # contact/CTA flows may opt in separately when product semantics exist.
        trusted_context.pop("public_contacts", None)
        tone_preferences = dict(trusted_context.pop("communication", {}))
        if profile.profile_status == "usable":
            tone_preferences.update(trusted_context.pop("content_preferences", {}))
        for claim in profile.context.claims:
            value = {
                "text": claim.text,
                "verification_status": claim.verification_status,
                "evidence_reference": claim.evidence_reference,
            }
            (verified if claim.verification_status == "verified" else unverified).append(value)
        revision = profile.revision
    return (
        trusted_context, tone_preferences, tuple(verified), tuple(unverified), revision,
    )
