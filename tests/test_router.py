from __future__ import annotations

import pytest

from app.routing.modules import Module
from app.routing.router import route_for_button, route_text
from app.routing.safety import SafetyLevel


@pytest.mark.parametrize(
    "text",
    [
        "Чем Loyalty Points отличаются от Travel Credits?",
        "Можно ли передать Travel Credits другому человеку?",
        "Что получает Silver с Elite Turbo?",
        "Какие выплаты у Ruby?",
        "Я точно буду получать $300 в день, если стану Ruby?",
        "Что такое MWR Life?",
        "Что должен знать новый партнёр?",
        "Как закрыть Silver?",
        "Что такое Elite Turbo?",
        "Что такое Guest Pass?",
    ],
)
def test_branded_knowledge_questions_route_to_travel_assistant(text: str) -> None:
    decision = route_text(text)
    assert decision.primary_module is Module.TRAVEL_ASSISTANT
    assert decision.is_uncertain is False


@pytest.mark.parametrize(
    "text",
    [
        "Ruby язык программирования",
        "Elite Dangerous",
        "баллы футбольного матча",
        "партнёр по бизнесу прислал договор",
    ],
)
def test_ambiguous_single_words_do_not_route_to_travel_assistant(text: str) -> None:
    assert route_text(text).primary_module is not Module.TRAVEL_ASSISTANT


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Напиши пост про Elite Turbo", Module.CONTENT_FACTORY),
        ("Покажи сигналы Lead Radar про Elite Turbo", Module.LEAD_RADAR),
        ("Подготовь инструкцию для нового партнёра", Module.PARTNER_PACKAGING),
    ],
)
def test_specialized_flow_priority_survives_new_topic_patterns(
    text: str, expected: Module,
) -> None:
    assert route_text(text).primary_module is expected


def test_post_routes_to_content_factory():
    d = route_text("Нужен пост о сомнениях перед поездкой")
    assert d.primary_module is Module.CONTENT_FACTORY
    assert d.safety_level is SafetyLevel.NOT_REQUIRED
    assert not d.is_mixed
    assert not d.is_uncertain


# --- Live prod-баг: root cause "Сделай план публикаций на 14 дней" не
# отвечал вообще. Причина — ни одно слово CONTENT_KEYWORDS не совпадало
# («план» намеренно не входит в CONTENT_KEYWORDS, иначе он перебивал бы
# предметные ASSISTANT_TOPIC_KEYWORDS вроде «тарифный план»), запрос уходил
# в Module.ORCHESTRATOR как is_uncertain. Общий regex-сигнал в
# CONTENT_PATTERNS покрывает весь класс «план/график/расписание
# публикаций/постов/контента», а не только эту формулировку. ---

def test_publication_plan_without_content_word_routes_to_content_factory():
    d = route_text("Сделай план публикаций на 14 дней")
    assert d.primary_module is Module.CONTENT_FACTORY
    assert not d.is_uncertain


def test_posting_schedule_phrasing_also_routes_to_content_factory():
    d = route_text("Нужен график постов на неделю")
    assert d.primary_module is Module.CONTENT_FACTORY
    assert not d.is_uncertain


def test_bare_plan_word_alone_does_not_force_content_factory():
    # "план" сам по себе не должен перебивать явный клиентский/тарифный
    # сигнал — только "план публикаций/постов/контента" — конкретный класс.
    d = route_text("Человек спрашивает, какой у вас тарифный план")
    assert d.primary_module is Module.TRAVEL_ASSISTANT


def test_client_question_routes_to_assistant():
    d = route_text("Человек спрашивает, чем Travel Advantage отличается от обычного поиска отелей")
    assert d.primary_module is Module.TRAVEL_ASSISTANT
    assert not d.is_mixed
    # Сравнение Travel Advantage с обычным поиском отелей → Safety Layer обязателен.
    assert d.safety_level is SafetyLevel.MANDATORY


def test_signals_route_to_lead_radar():
    d = route_text("Покажи свежие сигналы людей, которые ищут поездку")
    assert d.primary_module is Module.LEAD_RADAR


def test_safety_check_routes_to_safety_layer():
    d = route_text("Проверь этот текст перед публикацией")
    assert d.primary_module is Module.SAFETY_LAYER
    assert d.safety_level is SafetyLevel.MANDATORY


def test_partner_instruction_routes_to_packaging():
    d = route_text("Подготовь инструкцию для нового партнёра")
    assert d.primary_module is Module.PARTNER_PACKAGING


def test_commercial_offer_triggers_mandatory_safety():
    d = route_text("Сделай коммерческое предложение по настройке AI-инструмента")
    assert d.primary_module is Module.PARTNER_PACKAGING
    assert d.safety_level is SafetyLevel.MANDATORY


def test_payment_topic_triggers_mandatory_safety():
    d = route_text("Можно ли оплатить бронирование из России?")
    assert d.safety_level is SafetyLevel.MANDATORY


def test_mixed_task_is_decomposed():
    d = route_text("Нужен пост для клиента и найти сигналы интереса")
    assert d.is_mixed
    assert len(d.matched_modules) >= 2
    assert Module.LEAD_RADAR in d.matched_modules
    assert Module.CONTENT_FACTORY in d.matched_modules


def test_unclear_task_is_uncertain():
    d = route_text("просто что-то непонятное про абстракцию")
    assert d.is_uncertain
    assert d.primary_module is Module.ORCHESTRATOR


def test_button_forces_module():
    d = route_for_button(Module.CONTENT_FACTORY, "Сделай пост о новых направлениях")
    assert d.primary_module is Module.CONTENT_FACTORY
    assert not d.is_mixed
    assert not d.is_uncertain


def test_safety_button_forces_mandatory():
    d = route_for_button(Module.SAFETY_LAYER, "Вот текст: красивые горы")
    assert d.primary_module is Module.SAFETY_LAYER
    assert d.safety_level is SafetyLevel.MANDATORY


# --- Контент с предметными словами не должен становиться смешанным (требование 2) ---

def test_post_about_travel_advantage_is_content_only():
    d = route_text("Нужен пост о Travel Advantage")
    assert d.primary_module is Module.CONTENT_FACTORY
    assert not d.is_mixed


def test_post_about_tariffs_is_content_only_and_mandatory_safety():
    d = route_text("Нужен пост о тарифах Travel Advantage")
    assert d.primary_module is Module.CONTENT_FACTORY
    assert not d.is_mixed
    assert d.safety_level is SafetyLevel.MANDATORY


def test_reels_about_life_experiences_is_content_only():
    d = route_text("Сделай сценарий Reels о Life Experiences")
    assert d.primary_module is Module.CONTENT_FACTORY
    assert not d.is_mixed


# --- Контентные задачи не должны переключаться на AI Travel Assistant
#     из-за широких признаков "клиент" и "как устроен" (требование 3 корректировки) ---

def test_post_for_client_about_travel_advantage_is_content_only():
    d = route_text("Нужен пост для клиента о Travel Advantage")
    assert d.primary_module is Module.CONTENT_FACTORY
    assert not d.is_mixed


def test_post_about_how_travel_advantage_works_is_content_only():
    d = route_text("Сделай пост о том, как устроен Travel Advantage")
    assert d.primary_module is Module.CONTENT_FACTORY
    assert not d.is_mixed


# --- Усиленный Safety Layer для сравнений и личных/первых/холодных сообщений ---

def test_comparison_with_hotel_search_is_mandatory_safety():
    d = route_text("Человек спрашивает, чем Travel Advantage отличается от обычного поиска отелей")
    assert d.safety_level is SafetyLevel.MANDATORY


def test_personal_message_to_potential_partner_is_mandatory_safety():
    d = route_text("Нужно личное сообщение потенциальному партнёру")
    assert d.safety_level is SafetyLevel.MANDATORY


def test_first_message_to_potential_client_is_mandatory_safety():
    d = route_text("Подготовь первое сообщение потенциальному клиенту")
    assert d.safety_level is SafetyLevel.MANDATORY


def test_cold_message_is_mandatory_safety():
    d = route_text("Подготовь холодное сообщение для нового контакта")
    assert d.safety_level is SafetyLevel.MANDATORY


# --- Fix: явное действие пользователя (rewrite/adapt/shorten) в начале
# запроса не должно уступать Safety Layer только потому, что внутри
# ВСТАВЛЕННОГО/цитируемого материала встретилось тематическое или
# служебное слово вроде «проверить». Живой прод-баг: «Перепиши этот пост...:
# <пост, где внутри есть "Проверить сведения можно на сайте...">» роутился в
# Safety Layer и rewrite вообще не выполнялся. Принцип общий (не про
# конкретную тему поста) — проверяется на нейтральном и на sensitive-тексте
# одинаково. ---

_NEUTRAL_QUOTED_POST = (
    "Собрались в Грузию на пять дней. Забронировали отель в центре Тбилиси, "
    "взяли машину в аренду и заранее купили билеты на канатную дорогу."
)
_SENSITIVE_QUOTED_POST = (
    "Пришла повестка из военкомата. Юрист говорит, что запрет на выезд может "
    "быть оформлен через электронный реестр даже без личного вручения бумаги. "
    "Нужно проверить актуальные ограничения перед покупкой билетов."
)


def test_rewrite_neutral_quoted_post_routes_to_content_factory():
    d = route_text(f"Перепиши этот пост своими словами: {_NEUTRAL_QUOTED_POST}")
    assert d.primary_module is Module.CONTENT_FACTORY
    assert d.secondary_modules == ()


def test_rewrite_sensitive_quoted_post_still_routes_to_content_factory():
    """Регрессия: слово «проверить» внутри цитаты не должно отбирать
    приоритет у явной команды «Перепиши» в начале запроса."""
    d = route_text(f"Перепиши этот пост своими словами: {_SENSITIVE_QUOTED_POST}")
    assert d.primary_module is Module.CONTENT_FACTORY
    assert d.secondary_modules == ()


def test_adapt_for_vk_routes_to_content_factory():
    d = route_text(f"Адаптируй этот пост для ВК: {_NEUTRAL_QUOTED_POST}")
    assert d.primary_module is Module.CONTENT_FACTORY


def test_shorten_sensitive_quoted_post_routes_to_content_factory():
    d = route_text(f"Сократи этот пост: {_SENSITIVE_QUOTED_POST}")
    assert d.primary_module is Module.CONTENT_FACTORY


def test_explicit_risk_check_still_routes_to_safety_layer():
    """Не регрессия: явная просьба проверить (без rewrite-глагола) по-прежнему
    уходит в Safety Layer — это существующее корректное поведение."""
    d = route_text(f"Проверь этот пост на риски: {_SENSITIVE_QUOTED_POST}")
    assert d.primary_module is Module.SAFETY_LAYER
    assert Module.CONTENT_FACTORY in d.secondary_modules


def test_explicit_fact_check_still_routes_to_safety_layer():
    d = route_text(f"Проверь факты в этом посте: {_SENSITIVE_QUOTED_POST}")
    assert d.primary_module is Module.SAFETY_LAYER
    assert Module.CONTENT_FACTORY in d.secondary_modules


# --- Fix: live prod bug found in the 2a681a8+5c0ebca smoke test. An explicit
# leading rewrite action (перепиши/перефразируй/адаптируй/сократи) previously
# only suppressed a false-positive Safety score inside REWRITE_ACTION_KEYWORDS
# handling — it never contributed a score of its own. "Перепиши этот ТЕКСТ:
# <кейс>" has no "пост" (CONTENT_KEYWORDS' "переписат" stem does not match the
# imperative "перепиши") and, for many real pasted texts, no Safety/topic
# keyword either — so every score stayed at zero and the router fell back to
# Module.ORCHESTRATOR ("route not determined") instead of running the
# rewrite. The fix makes the leading rewrite action a self-sufficient Content
# Factory signal, independent of whether "текст"/"пост"/"публикация" or no
# label at all follows it. ---

_REAL_ANTALYA_REWRITE_CASE = (
    "Перепиши этот текст так, что бы меня не обвинили в плагиате: Свежий "
    "кейс! \nОтправили брата с женой в путешествие в Анталию, за отель "
    "отдали 47 т.р. вместо 113 на Букинге, трансфер тоже в 3 раза дешевле. \n"
    "В общем от начальной суммы путешествия сэкономили им почти 90 тысяч. \n"
    "Они до сих пор не верят: А, что так можно было?\n\n"
    "Хотя ни брат, ни его жена партнерами клуба не являются (но уже очень "
    "хотят ими стать), они записаны в аккаунт, как гости-пассажиры! \n"
    "Прилетели довольные, теперь всем рассказывают, что сказочно отдохнули "
    "и уже планируют поездку снова!  \nИ, оказалось, что вот так тоже "
    "можно! \nБез баллов, без парнерства, без взносов, без заморочек, со "
    "скидками в 67%…просто папа решил сделать ребенку подарок и записал "
    "его в свой аккаунт! \nЧудеса!"
)


def test_real_production_rewrite_of_bare_text_no_longer_uncertain():
    """Regression: the exact real production text that previously routed to
    Module.ORCHESTRATOR (is_uncertain=True) must now run as a rewrite task."""
    d = route_text(_REAL_ANTALYA_REWRITE_CASE)
    assert d.primary_module is Module.CONTENT_FACTORY
    assert d.secondary_modules == ()
    assert not d.is_uncertain


@pytest.mark.parametrize("verb", ["Перепиши", "Перефразируй", "Адаптируй", "Сократи"])
def test_rewrite_verb_on_bare_text_label_routes_to_content_factory(verb):
    """None of "текст"/no-label bodies are in CONTENT_KEYWORDS - the rewrite
    verb itself must be enough, not just when paired with "пост"."""
    d = route_text(f"{verb} этот текст: {_NEUTRAL_QUOTED_POST}")
    assert d.primary_module is Module.CONTENT_FACTORY
    assert not d.is_uncertain


def test_rephrase_bare_colon_routes_to_content_factory():
    d = route_text(f"Перефразируй: {_NEUTRAL_QUOTED_POST}")
    assert d.primary_module is Module.CONTENT_FACTORY
    assert not d.is_uncertain


def test_shorten_bare_text_label_routes_to_content_factory():
    d = route_text(f"Сократи текст: {_NEUTRAL_QUOTED_POST}")
    assert d.primary_module is Module.CONTENT_FACTORY
    assert not d.is_uncertain


def test_explicit_check_on_bare_text_label_still_routes_to_safety_layer():
    """Not a regression: "Проверь" has no rewrite verb, so it must keep
    routing to Safety Layer exactly as before - the new rewrite-action score
    must not leak into check-only requests."""
    d = route_text(f"Проверь этот текст: {_NEUTRAL_QUOTED_POST}")
    assert d.primary_module is Module.SAFETY_LAYER


def test_check_and_rewrite_both_requested_keeps_safety_priority():
    """Если ведущая инструкция сама содержит и rewrite-, и check-глагол —
    поведение не должно измениться (fallback на полный текст, Safety не
    подавляется)."""
    d = route_text(f"Проверь и перепиши этот пост: {_SENSITIVE_QUOTED_POST}")
    assert d.primary_module is Module.SAFETY_LAYER


def test_real_production_defect2_case_now_routes_to_content_factory():
    """Точный текст реального прод-инцидента (см. review): «Перепиши этот
    пост так что бы в плагиате не обвинили: <пост про повестку, где внутри
    встречается 'Проверить сведения...'>» — раньше уходил в Safety Layer и
    rewrite не выполнялся."""
    real_text = (
        "Перепиши этот пост так что бы в плагиате не обвинили:🫡ПРИШЛА "
        "ПОВЕСТКА — МОЖНО ЛИ ВЫЕХАТЬ ИЗ РОССИИ?\n\n"
        "Проверить сведения можно на официальном сайте реестрповесток.рф"
    )
    d = route_text(real_text)
    assert d.primary_module is Module.CONTENT_FACTORY


def test_rewrite_keyword_alone_without_quoted_content_is_unaffected():
    """Обычная короткая команда без вставленного материала — поведение не
    меняется (rewrite-глаголы и раньше вели в Content Factory через «пост»)."""
    d = route_text("Перепиши этот пост")
    assert d.primary_module is Module.CONTENT_FACTORY
