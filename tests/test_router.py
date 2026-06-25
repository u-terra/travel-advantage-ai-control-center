from __future__ import annotations

from app.routing.modules import Module
from app.routing.router import route_for_button, route_text
from app.routing.safety import SafetyLevel


def test_post_routes_to_content_factory():
    d = route_text("Нужен пост о сомнениях перед поездкой")
    assert d.primary_module is Module.CONTENT_FACTORY
    assert d.safety_level is SafetyLevel.NOT_REQUIRED
    assert not d.is_mixed
    assert not d.is_uncertain


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
