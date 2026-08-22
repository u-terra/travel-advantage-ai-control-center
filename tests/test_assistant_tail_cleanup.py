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
