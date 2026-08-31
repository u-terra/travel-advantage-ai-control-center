from __future__ import annotations

import pytest

from app.routing.safety import SafetyLevel, detect_safety_level


@pytest.mark.parametrize(
    "phrase",
    [
        "доход от партнёрства",
        "хочу обсудить заработок",
        "сравнение с booking",
        "скидка для клиента",
        "оплата криптовалютой",
        "тарифы Travel Advantage",
        "первое сообщение клиенту",
        "коммерческое предложение",
        "написать клиенту по бронированию",
        # Сравнения с обычными сервисами / Booking / Airbnb / турагентами
        "чем travel advantage отличается от обычного поиска отелей",
        "сравнить travel advantage с booking",
        "по сравнению с airbnb",
        "другой сервис для поиска",
        "стоит ли идти к турагенту",
        # Личные / первые / холодные сообщения потенциальному клиенту/партнёру
        "личное сообщение потенциальному партнёру",
        "первое сообщение потенциальному клиенту",
        "холодное сообщение новому контакту",
        # Холодный / новый контакт сам по себе
        "подготовь холодный контакт",
        "напомни про новый контакт",
    ],
)
def test_mandatory_safety_topics(phrase: str) -> None:
    assert detect_safety_level(phrase.lower()) is SafetyLevel.MANDATORY


@pytest.mark.parametrize(
    "phrase",
    [
        "сценарий ролика для подписчиков",
        "ответ на возражение для подписчика",
        "презентация продукта",
        "reels про новые места",
    ],
)
def test_recommended_safety_topics(phrase: str) -> None:
    assert detect_safety_level(phrase.lower()) is SafetyLevel.RECOMMENDED


def test_no_sensitive_topic_not_required() -> None:
    assert detect_safety_level("пост о красивых горах") is SafetyLevel.NOT_REQUIRED


# --- Fix: название/слоган в кавычках не должно давать ложный MANDATORY ---
# (живой прод-баг: «Путешествуй выгодно» как название группы ловилось на
# подстроке "выгод" из MANDATORY_SAFETY_KEYWORDS)

def test_quoted_brand_name_with_vygod_substring_is_not_mandatory() -> None:
    text = (
        "разработай стратегию ведения моей группы вконтакте «путешествуй "
        "выгодно». нужны рубрики и контент-план на 2 недели"
    )
    assert detect_safety_level(text) is SafetyLevel.NOT_REQUIRED


def test_real_financial_benefit_request_outside_quotes_stays_mandatory() -> None:
    text = "расскажи, какая финансовая выгода и тариф для клиента при бронировании"
    assert detect_safety_level(text) is SafetyLevel.MANDATORY


def test_quoted_span_does_not_hide_mandatory_keyword_in_open_text() -> None:
    # Кавычки вырезают только сам процитированный фрагмент — если keyword
    # встречается вне кавычек, поведение не меняется.
    text = 'группа «путешествуй легко», но у нас выгодный тариф для клиента'
    assert detect_safety_level(text) is SafetyLevel.MANDATORY


# --- Fix: "Explicit Rewrite must mean Rewrite" live prod bug. A plain rewrite
# of the user's own already-published post ("Экскурсия... за 1,5$... Скидка
# ...-93%") was tripping MANDATORY via ordinary marketing vocabulary
# (скидк/стоимост/цен) that appears in essentially any travel-deal post,
# turning a text-transformation task into an unwanted fact-check. high_risk_only
# narrows detection to genuinely high-risk claims only, for callers (the
# router's has_rewrite_action gate) that already know this is a rewrite of
# user-supplied source material, not a request to invent/verify new claims.

@pytest.mark.parametrize(
    "phrase",
    [
        "экскурсия с частным гидом за 1,5$ на человека",
        "минимальная стоимость такой экскурсии на других платформах 17,5€",
        "скидка с учётом баллов лояльности -93%",
        "тарифы и бронирование отеля",
        "оплата картой, доступность мест",
        "сравнение с booking и airbnb",
    ],
)
def test_high_risk_only_ignores_ordinary_price_and_discount_vocabulary(phrase: str) -> None:
    assert detect_safety_level(phrase.lower(), high_risk_only=True) is SafetyLevel.NOT_REQUIRED


@pytest.mark.parametrize(
    "phrase",
    [
        "доход гарантирован уже через месяц",
        "гарантированный доход 100% без риска",
        "поставим точный диагноз по фото",
        "юридическая консультация по вашему делу гарантирована",
        "нелегальный въезд без визы",
    ],
)
def test_high_risk_only_still_catches_genuinely_dangerous_claims(phrase: str) -> None:
    assert detect_safety_level(phrase.lower(), high_risk_only=True) is SafetyLevel.MANDATORY


def test_high_risk_only_does_not_change_default_behavior() -> None:
    text = "скидка для клиента"
    assert detect_safety_level(text) is SafetyLevel.MANDATORY
    assert detect_safety_level(text, high_risk_only=False) is SafetyLevel.MANDATORY
