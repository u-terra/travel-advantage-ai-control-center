from __future__ import annotations

import re
from enum import Enum


class SafetyLevel(str, Enum):
    NOT_REQUIRED = "не требуется"
    RECOMMENDED = "рекомендуется"
    MANDATORY = "обязателен"


# Префиксы, требующие привязки к началу слова (\b), чтобы исключить ложные
# совпадения внутри других слов. «цен» покрывает формы цена, цены, цене, цену,
# ценой, ценам — и при этом не цепляет «сценарий» / «оценить».
_MANDATORY_PRICE_PATTERN = re.compile(r"\bцен")

# Название/слоган в кавычках («Путешествуй выгодно», "Название группы") —
# собственное имя, а не утверждение или обещание от лица бота: keyword внутри
# него не должен приравниваться к такому же слову в основном тексте запроса
# (ср. «Разработай стратегию для группы «Путешествуй выгодно»» — MANDATORY не
# нужен — и «Расскажи про выгодный тариф» — MANDATORY нужен). Вырезается
# только перед keyword-проверкой, сам текст задачи не меняется.
_QUOTED_SPAN_PATTERN = re.compile(r"«[^»]*»|\"[^\"]*\"")


def _without_quoted_spans(text_lower: str) -> str:
    return _QUOTED_SPAN_PATTERN.sub(" ", text_lower)


# Темы, при которых Safety Layer обязателен (см. п.7 ТЗ).
MANDATORY_SAFETY_KEYWORDS: tuple[str, ...] = (
    # доход, выгода
    "доход", "заработ", "окупаем",
    "скидк", "выгод", "эконом",
    # тарифы и явные ценовые фразы (общий префикс «цен» проверяется отдельно через \b)
    "стоимост", "сколько стоит",
    "тариф",
    # доступность, бронирование, оплата
    "доступност",
    "брониров", "бронь",
    "оплат", "способ оплат",
    "криптовалют", "крипта", "крипто",
    # партнёрская модель
    "партнёрск", "партнерск", "партнёрств", "партнерств",
    # сравнения Travel Advantage с обычными сервисами / Booking / Airbnb / турагентами
    "отличается", "отличаются", "отличие от",
    "сравнить", "сравнение с", "по сравнению с",
    "против booking", "vs booking", "vs airbnb", "лучше чем booking",
    "другой сервис", "другие сервис",
    "обычный поиск отелей", "поиск отелей",
    "booking", "airbnb",
    "турагент",
    # коммерческие материалы
    "коммерческое предложение",
    # личные, первые, холодные сообщения потенциальному клиенту/партнёру/новому контакту
    "первое сообщение", "личное сообщение", "холодное сообщение",
    "холодный контакт", "новый контакт",
    "потенциальному клиент", "потенциальному партн",
    "потенциальный клиент", "потенциальный партн",
    "потенциальным клиент", "потенциальным партн",
    "написать клиент", "написать партн", "написать человек",
)

# Темы, при которых Safety Layer рекомендуется, но не обязателен.
RECOMMENDED_SAFETY_KEYWORDS: tuple[str, ...] = (
    "reels", "рилс", "рилз",
    "stories", "сторис",
    "сценар",
    "ответ на возражен", "возражен",
    "презентац",
    "инструкц",
    "faq",
)

# Live prod bug: rewrite/paraphrase requests ("Перепиши этот пост, чтобы не
# обвинили в плагиате: <пользовательский пост с ценами/скидками>") were
# scored against the SAME broad MANDATORY_SAFETY_KEYWORDS as brand-new content
# creation — "скидк"/"стоимост"/"тариф"/"брониров"/"оплат"/price-mentions are
# ordinary vocabulary for any travel-deal post and fire on nearly every real
# example, turning a pure text-transformation task into an unwanted
# fact-check. When the user is asking to rewrite THEIR OWN already-published
# claims (not asking the bot to invent/verify new commercial claims), those
# numbers/discounts are source material to preserve, not a new promise to
# police — see app/routing/router.py's has_rewrite_action gate, which is the
# caller that decides when to pass high_risk_only=True.
#
# This list intentionally stays narrow and does NOT replace
# MANDATORY_SAFETY_KEYWORDS for any other flow: genuinely high-risk content
# (guaranteed income, dangerous financial promises, medical/legal advice,
# dangerous actions) must still block a rewrite exactly like it blocks new
# content — only the broad topical/marketing vocabulary is exempted.
HIGH_RISK_SAFETY_KEYWORDS: tuple[str, ...] = (
    # гарантированный доход / опасные финансовые обещания. "гарант" (not a
    # longer stem) is deliberate: доход гарантирован / гарантированный доход /
    # гарантирую / гарантия дохода all share this root, but differ in suffix
    # (short-form predicate adjective vs full adjective vs verb vs noun) - a
    # longer stem like "гарантированн" misses the short predicate form
    # "гарантирован" entirely (see test_free_text_safety_gated_content_task_
    # generates_then_checks_draft, a live prod case with exactly that phrase).
    "гарант",
    "100% доход", "без риска", "без вложен",
    # медицинский / high-risk юридический совет
    "диагноз", "лечени", "медицинск", "противопоказан",
    "юридическ", "иммиграционн", "виза гарантир", "гражданств гарантир",
    # явно опасные действия
    "нелегальн", "незаконн", "без визы", "в обход закон",
)


def detect_safety_level(
    text_lower: str, *, high_risk_only: bool = False,
) -> SafetyLevel:
    """Определяет уровень Safety Layer по содержимому текста (lower-cased).

    ``high_risk_only`` — для явных rewrite/paraphrase-задач (см.
    HIGH_RISK_SAFETY_KEYWORDS выше): проверяет только реально существенный
    риск, не топ-словарь обычного travel-контента.
    """
    unquoted = _without_quoted_spans(text_lower)
    if high_risk_only:
        for kw in HIGH_RISK_SAFETY_KEYWORDS:
            if kw in unquoted:
                return SafetyLevel.MANDATORY
        return SafetyLevel.NOT_REQUIRED
    if _MANDATORY_PRICE_PATTERN.search(unquoted):
        return SafetyLevel.MANDATORY
    for kw in MANDATORY_SAFETY_KEYWORDS:
        if kw in unquoted:
            return SafetyLevel.MANDATORY
    for kw in RECOMMENDED_SAFETY_KEYWORDS:
        if kw in unquoted:
            return SafetyLevel.RECOMMENDED
    return SafetyLevel.NOT_REQUIRED
