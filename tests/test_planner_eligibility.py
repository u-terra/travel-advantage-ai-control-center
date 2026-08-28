from __future__ import annotations

import pytest

from app.planner.eligibility import is_planner_allowed_for_user, is_planner_eligible


@pytest.mark.parametrize(
    "text",
    [
        "Проанализируй конкурента https://example.com и предложи, что нам делать дальше",
        "Разбери сайт конкурента и сравни его позиционирование с нашим",
        "Изучи конкурента и оцени, чем мы можем выделиться",
        "Проанализируй конкурента ТурКлуб и скажи, что мне делать лучше него",
    ],
)
def test_competitor_analysis_is_eligible(text):
    assert is_planner_eligible(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Проанализируй https://example.com и предложи, как нам отстроиться",
        "Изучи https://competitor.example.com/pricing и сравни с нашими ценами",
    ],
)
def test_url_analysis_request_is_eligible_without_the_word_competitor(text):
    assert is_planner_eligible(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Напиши пост про акцию, затем проверь его на риски и подготовь комплект для партнёра",
        "1. Собери конкурентов\n2. Проверь их сайты\n3. Подготовь сравнение",
        "Сначала напиши черновик ответа клиенту, а потом проверь его и оформи комплект",
    ],
)
def test_multi_action_content_task_is_eligible(text):
    assert is_planner_eligible(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Проведи многошаговое исследование рынка Турции по нескольким источникам и сделай выводы",
        "Собери информацию из нескольких источников про спрос на туры в Египет",
    ],
)
def test_multistep_research_is_eligible(text):
    assert is_planner_eligible(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Напиши пост про раннее бронирование отеля в Турции для инстаграма",
        "Сделай контент-план на 2 недели по нашим турам",
        "Придумай 10 идей для Reels про пляжный отдых",
        "Ответь клиенту, который спрашивает про визу в Турцию",
        "Проверь этот текст на рискованные формулировки: скидка 50% всем",
        "Напиши длинный подробный пост про преимущества раннего бронирования, "
        "распиши все нюансы, приведи примеры и сделай его максимально развёрнутым",
    ],
)
def test_simple_requests_are_not_eligible_even_if_long(text):
    assert is_planner_eligible(text) is False


@pytest.mark.parametrize("text", ["", "   ", None, 42])
def test_empty_or_non_string_input_is_not_eligible(text):
    assert is_planner_eligible(text) is False


# ── user allowlist gate (staged rollout, separate axis from task content) ──


def test_allowed_user_passes_the_gate():
    assert is_planner_allowed_for_user(100, frozenset({100, 200})) is True


def test_user_not_in_allowlist_is_denied():
    assert is_planner_allowed_for_user(999, frozenset({100, 200})) is False


def test_empty_allowlist_denies_everyone_fail_closed():
    """Critical staged-rollout property: an empty/unset allowlist must NOT
    be read as 'no restriction' - it must deny everyone."""
    assert is_planner_allowed_for_user(100, frozenset()) is False


def test_missing_telegram_user_id_is_denied():
    assert is_planner_allowed_for_user(None, frozenset({100})) is False
