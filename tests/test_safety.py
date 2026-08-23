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
