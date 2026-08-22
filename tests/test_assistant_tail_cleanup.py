from __future__ import annotations

from app.services.assistant_tail_cleanup import strip_assistant_tail


def test_strips_trailing_mogu_sravnit_varianty():
    text = (
        "Если выбирать между поездом и самолётом, лучше смотреть на "
        "конкретную поездку, а не на привычку.\n\nМогу сравнить варианты."
    )
    result = strip_assistant_tail(text)
    assert "Могу сравнить" not in result
    assert result.startswith(
        "Если выбирать между поездом и самолётом, лучше смотреть на "
        "конкретную поездку, а не на привычку."
    )


def test_strips_trailing_if_you_want_mozhem_proverit_otel_i_daty():
    text = (
        "Отель уже забронирован на нужные даты.\n\n"
        "Если хотите, можем вместе проверить конкретный отель и даты."
    )
    result = strip_assistant_tail(text)
    assert "можем вместе проверить" not in result
    assert "Отель уже забронирован на нужные даты." in result


def test_preserves_normal_human_cta_skazhite_daty():
    text = "Билеты дорожают ближе к вылету. Скажите даты — посмотрю варианты."
    assert strip_assistant_tail(text) == text


def test_preserves_ordinary_post_without_tail_unchanged():
    text = (
        "Египетские пирамиды остаются одним из самых узнаваемых символов "
        "древнего мира. Для бронирования переходите по ссылке."
    )
    assert strip_assistant_tail(text) == text


def test_does_not_remove_assistant_like_phrase_from_middle_of_text():
    text = (
        "Я могу долго рассказывать про Карелию, но вот главное: маршрут "
        "начинается от Питера."
    )
    assert strip_assistant_tail(text) == text


def test_preserves_other_natural_ctas():
    for text in (
        "Напишите даты и город — проверю конкретные варианты.",
        "Для бронирования переходите по ссылке.",
        "Какой вариант выбрали бы вы?",
    ):
        assert strip_assistant_tail(text) == text


def test_strips_mogu_pomoch():
    text = "Вот короткий ответ по вашему вопросу. Могу помочь."
    result = strip_assistant_tail(text)
    assert "Могу помочь" not in result
    assert "Вот короткий ответ по вашему вопросу." in result


def test_strips_if_you_want_mogu_sravnit():
    text = "Маршрут через Стамбул обычно дешевле. Если хотите, могу сравнить варианты."
    result = strip_assistant_tail(text)
    assert "могу сравнить" not in result.lower()
    assert "Маршрут через Стамбул обычно дешевле." in result


def test_strips_if_ty_hochesh_mogu_pomoch():
    text = "Цены на туры выросли в этом сезоне. Если хочешь, могу помочь с этим."
    result = strip_assistant_tail(text)
    assert "могу помочь" not in result.lower()
    assert "Цены на туры выросли в этом сезоне." in result


def test_never_returns_empty_text():
    text = "Могу помочь с этим."
    assert strip_assistant_tail(text) == text


def test_empty_text_returns_empty_text():
    assert strip_assistant_tail("") == ""


def test_collapses_trailing_blank_lines_after_removal():
    text = "Основной текст поста.\n\n\nМогу сравнить варианты.\n"
    result = strip_assistant_tail(text)
    assert result == "Основной текст поста."


# --- production regression: "если нужно" + "можно + сервисное действие" ---


def test_strips_if_nuzhno_mogu_pomoch_sravnit_marshrut():
    text = (
        "Поезд удобнее самолёта на коротких расстояниях по билетам и багажу.\n\n"
        "Если нужно, могу помочь сравнить варианты для конкретного маршрута."
    )
    result = strip_assistant_tail(text)
    assert "могу помочь сравнить" not in result.lower()
    assert (
        "Поезд удобнее самолёта на коротких расстояниях по билетам и багажу."
        in result
    )


def test_strips_if_hotite_mozhno_razobrat_na_primere_otelya():
    text = (
        "Оплата возможна несколькими способами в зависимости от направления.\n\n"
        "Если хотите, можно сразу разобрать на примере конкретного отеля и дат."
    )
    result = strip_assistant_tail(text)
    assert "разобрать" not in result.lower()
    assert (
        "Оплата возможна несколькими способами в зависимости от направления."
        in result
    )


def test_strips_if_hochesh_mozhno_posmotret_varianty():
    text = "Цены на туры выросли в этом сезоне. Если хочешь, можно посмотреть варианты."
    result = strip_assistant_tail(text)
    assert "посмотреть варианты" not in result.lower()
    assert "Цены на туры выросли в этом сезоне." in result


def test_strips_if_nuzhno_mogu_pomoch_short():
    text = "Вот короткий ответ по вашему вопросу. Если нужно, могу помочь."
    result = strip_assistant_tail(text)
    assert "могу помочь" not in result.lower()
    assert "Вот короткий ответ по вашему вопросу." in result


def test_strips_if_hotite_mozhno_proverit_konkretny_variant():
    text = "Маршрут через Стамбул обычно дешевле. Если хотите, можно проверить конкретный вариант."
    result = strip_assistant_tail(text)
    assert "проверить конкретный вариант" not in result.lower()
    assert "Маршрут через Стамбул обычно дешевле." in result


def test_preserves_if_hotite_mozhno_zabronirovat_po_ssylke():
    text = "Билеты дорожают ближе к вылету. Если хотите, можно забронировать по этой ссылке."
    assert strip_assistant_tail(text) == text


def test_preserves_if_nuzhno_napishite_daty():
    text = "Маршрут через Стамбул обычно дешевле. Если нужно, напишите даты."
    assert strip_assistant_tail(text) == text
