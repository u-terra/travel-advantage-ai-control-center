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
