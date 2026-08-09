"""Тесты контракта Content Intelligence v1.

Реальный LLM здесь не вызывается: проверяется только словарь значений.
"""

from __future__ import annotations

import pytest

from app.domain.content_intelligence import (
    ACTION_OBSERVE,
    ACTION_SKIP,
    KIND_CASE_OR_REVIEW,
    KIND_COMPETITOR_SIGNAL,
    KIND_NEWS,
    KIND_NOISE,
    KIND_POST_IDEA,
    KIND_SCRIPT_IDEA,
    MATERIAL_ACTIONS,
    MATERIAL_KINDS,
    MaterialClassification,
    default_action_for,
    normalize_material_action,
    normalize_material_kind,
)
from app.services.llm.models import SourceAnalysisPayload


def test_all_planned_kinds_exist() -> None:
    assert set(MATERIAL_KINDS) == {
        KIND_NEWS,
        KIND_POST_IDEA,
        KIND_SCRIPT_IDEA,
        KIND_CASE_OR_REVIEW,
        KIND_COMPETITOR_SIGNAL,
        KIND_NOISE,
    }


def test_all_planned_actions_exist() -> None:
    assert set(MATERIAL_ACTIONS) == {
        "make_post",
        "make_script",
        "generate_topics",
        "adapt_for_travel_advantage",
        "observe",
        "skip",
    }


def test_every_kind_has_a_default_action() -> None:
    for kind in MATERIAL_KINDS:
        assert default_action_for(kind) in MATERIAL_ACTIONS


def test_noise_is_not_turned_into_work() -> None:
    assert default_action_for(KIND_NOISE) == ACTION_SKIP


def test_competitor_signal_is_only_observed() -> None:
    # Сигнал конкурента — повод посмотреть, а не повод сразу что-то писать.
    assert default_action_for(KIND_COMPETITOR_SIGNAL) == ACTION_OBSERVE


def test_unknown_kind_falls_back_to_observing() -> None:
    # Непонятый материал безопаснее наблюдать, чем обрабатывать.
    assert default_action_for("что-то новое") == ACTION_OBSERVE


@pytest.mark.parametrize("raw", ["news", " NEWS ", "News"])
def test_kind_normalization_is_case_insensitive(raw: str) -> None:
    assert normalize_material_kind(raw) == KIND_NEWS


@pytest.mark.parametrize("raw", ["", "   ", "unknown_kind", "новость", None])
def test_unknown_kind_normalizes_to_empty(raw) -> None:
    assert normalize_material_kind(raw) == ""


@pytest.mark.parametrize("raw", ["", "publish_now", "удалить", None])
def test_unknown_action_normalizes_to_empty(raw) -> None:
    assert normalize_material_action(raw) == ""


def test_classification_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError):
        MaterialClassification(kind="unknown_kind", action="make_post")


def test_classification_rejects_an_unknown_action() -> None:
    with pytest.raises(ValueError):
        MaterialClassification(kind=KIND_NEWS, action="publish_now")


def test_classification_is_immutable() -> None:
    classification = MaterialClassification(kind=KIND_NEWS, action=ACTION_OBSERVE)

    with pytest.raises(Exception):
        classification.kind = KIND_NOISE  # type: ignore[misc]


def test_from_raw_fills_the_action_by_kind() -> None:
    classification = MaterialClassification.from_raw(kind="script_idea")

    assert classification is not None
    assert classification.action == "make_script"


def test_from_raw_keeps_an_explicit_action() -> None:
    classification = MaterialClassification.from_raw(
        kind="news", action="make_post", rationale="  повод для поста  "
    )

    assert classification is not None
    assert (classification.action, classification.rationale) == (
        "make_post",
        "повод для поста",
    )


def test_from_raw_drops_an_unknown_action_instead_of_keeping_it() -> None:
    classification = MaterialClassification.from_raw(kind="noise", action="publish")

    assert classification is not None
    assert classification.action == ACTION_SKIP


def test_from_raw_returns_none_for_an_unknown_kind() -> None:
    assert MaterialClassification.from_raw(kind="что-то") is None


def test_actionable_flag() -> None:
    assert MaterialClassification(kind=KIND_POST_IDEA, action="make_post").is_actionable
    assert not MaterialClassification(
        kind=KIND_NOISE, action=ACTION_SKIP
    ).is_actionable
    assert not MaterialClassification(
        kind=KIND_COMPETITOR_SIGNAL, action=ACTION_OBSERVE
    ).is_actionable


def test_classification_can_point_at_a_registry_source() -> None:
    classification = MaterialClassification.from_raw(
        kind="case_or_review", source_id="some_registry_id"
    )

    assert classification is not None
    assert classification.source_id == "some_registry_id"


# ── Совместимость с существующим контрактом анализа ──────────────────────────


def _payload(**overrides) -> SourceAnalysisPayload:
    values = dict(
        summary="Кратко",
        key_facts=(),
        disputed_claims=(),
        audience_value="Ценность",
        target_audiences=(),
        content_angles=(),
        recommended_formats=(),
        warnings=(),
    )
    values.update(overrides)
    return SourceAnalysisPayload(**values)


def test_existing_analysis_payload_works_without_classification() -> None:
    # Поле необязательное: адаптеры провайдеров не переписывались.
    assert _payload().classification is None


def test_analysis_payload_can_carry_a_classification() -> None:
    classification = MaterialClassification.from_raw(kind="post_idea")

    assert _payload(classification=classification).classification is classification
