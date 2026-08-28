from __future__ import annotations

import pytest

from app.planner.plan import (
    ALLOWED_EXECUTORS,
    EXECUTOR_CATALOG,
    MAX_STEPS,
    InvalidTaskPlanError,
    PlanStep,
    TaskPlan,
    validate_task_plan,
)


def test_executor_catalog_matches_allowed_executors():
    assert set(EXECUTOR_CATALOG) == ALLOWED_EXECUTORS


def _step(**overrides):
    base = dict(
        id="step_1",
        action="Собрать список конкурентов",
        executor="list_competitors",
        input={},
        depends_on=[],
    )
    base.update(overrides)
    return base


def _raw(steps=None, **overrides):
    base = dict(
        goal="Проанализировать конкурента и предложить действия",
        reason="Пользователь явно попросил анализ конкурента",
        steps=steps if steps is not None else [_step()],
        final_output="Итоговые рекомендации по конкуренту",
    )
    base.update(overrides)
    return base


def test_valid_plan_round_trips():
    plan = validate_task_plan(_raw())
    assert isinstance(plan, TaskPlan)
    assert plan.goal.startswith("Проанализировать")
    assert plan.final_output == "Итоговые рекомендации по конкуренту"
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert isinstance(step, PlanStep)
    assert step.id == "step_1"
    assert step.executor == "list_competitors"
    assert step.depends_on == ()


def test_valid_multi_step_plan_with_dependencies():
    steps = [
        _step(id="step_1", executor="list_competitors", depends_on=[]),
        _step(id="step_2", executor="fetch_public_source", depends_on=["step_1"]),
        _step(id="step_3", executor="analyze_source", depends_on=["step_2"]),
        _step(id="step_4", executor="generate_content", depends_on=["step_3"]),
    ]
    plan = validate_task_plan(_raw(steps=steps))
    assert [s.id for s in plan.steps] == ["step_1", "step_2", "step_3", "step_4"]
    assert plan.steps[-1].depends_on == ("step_3",)


def test_all_allowed_executors_are_individually_accepted():
    for executor in ALLOWED_EXECUTORS:
        plan = validate_task_plan(_raw(steps=[_step(id="step_1", executor=executor)]))
        assert plan.steps[0].executor == executor


def test_fetch_public_source_is_a_valid_executor_id():
    """Phase 1 requirement: the contract must already accept
    fetch_public_source even though no live implementation exists yet."""
    assert "fetch_public_source" in ALLOWED_EXECUTORS
    plan = validate_task_plan(
        _raw(steps=[_step(id="step_1", executor="fetch_public_source", input={"url": "https://example.com"})])
    )
    assert plan.steps[0].executor == "fetch_public_source"
    assert plan.steps[0].input["url"] == "https://example.com"


def test_unknown_executor_is_rejected():
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(_raw(steps=[_step(executor="do_whatever_the_user_wants")]))


def test_too_many_steps_is_rejected():
    steps = [
        _step(id=f"step_{i}", executor="check_safety") for i in range(MAX_STEPS + 1)
    ]
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(_raw(steps=steps))


def test_max_steps_boundary_is_accepted():
    steps = [_step(id=f"step_{i}", executor="check_safety") for i in range(MAX_STEPS)]
    plan = validate_task_plan(_raw(steps=steps))
    assert len(plan.steps) == MAX_STEPS


def test_duplicate_step_id_is_rejected():
    steps = [_step(id="step_1"), _step(id="step_1", executor="check_safety")]
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(_raw(steps=steps))


def test_unknown_dependency_is_rejected():
    steps = [_step(id="step_1", depends_on=["step_missing"])]
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(_raw(steps=steps))


def test_forward_dependency_is_rejected():
    steps = [
        _step(id="step_1", depends_on=["step_2"]),
        _step(id="step_2", executor="check_safety"),
    ]
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(_raw(steps=steps))


def test_self_dependency_is_rejected():
    steps = [_step(id="step_1", depends_on=["step_1"])]
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(_raw(steps=steps))


def test_duplicate_depends_on_entries_rejected():
    steps = [
        _step(id="step_1"),
        _step(id="step_2", executor="check_safety", depends_on=["step_1", "step_1"]),
    ]
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(_raw(steps=steps))


def test_empty_steps_list_is_rejected():
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(_raw(steps=[]))


def test_steps_not_a_list_is_rejected():
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(_raw(steps={"id": "step_1"}))


def test_step_not_a_dict_is_rejected():
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(_raw(steps=["step_1"]))


@pytest.mark.parametrize("bad_id", ["", "   ", "step 1", "step/1", "шаг_1"])
def test_invalid_step_id_is_rejected(bad_id):
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(_raw(steps=[_step(id=bad_id)]))


@pytest.mark.parametrize("key", ["goal", "reason", "final_output"])
def test_missing_required_top_level_field_is_rejected(key):
    raw = _raw()
    del raw[key]
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(raw)


@pytest.mark.parametrize("key", ["goal", "reason", "final_output"])
def test_empty_required_top_level_field_is_rejected(key):
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(_raw(**{key: "   "}))


def test_missing_action_is_rejected():
    steps = [_step()]
    del steps[0]["action"]
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(_raw(steps=steps))


def test_step_input_defaults_to_empty_mapping():
    steps = [_step()]
    del steps[0]["input"]
    plan = validate_task_plan(_raw(steps=steps))
    assert dict(plan.steps[0].input) == {}


def test_step_input_is_json_safe_and_frozen():
    plan = validate_task_plan(
        _raw(steps=[_step(input={"url": "https://example.com", "n": 3, "nested": {"a": 1}})])
    )
    frozen_input = plan.steps[0].input
    assert frozen_input["url"] == "https://example.com"
    assert frozen_input["nested"]["a"] == 1
    with pytest.raises(TypeError):
        frozen_input["url"] = "changed"  # MappingProxyType is read-only


def test_step_input_rejects_non_json_safe_values():
    class Weird:
        pass

    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(_raw(steps=[_step(input={"bad": Weird()})]))


def test_step_input_not_a_dict_is_rejected():
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(_raw(steps=[_step(input="not a dict")]))


def test_depends_on_not_a_list_is_rejected():
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(_raw(steps=[_step(depends_on="step_0")]))


@pytest.mark.parametrize("raw", [None, "not a dict", 42, ["list"]])
def test_non_dict_input_is_rejected(raw):
    with pytest.raises(InvalidTaskPlanError):
        validate_task_plan(raw)
