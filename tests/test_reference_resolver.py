from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.domain.action_contract import ActionContract
from app.routing.modules import Module
from app.services.knowledge_service import KnowledgeBundle, KnowledgeService
from app.services.reference_resolver import ReferenceResolver, ResolvedActionContext
from tests.test_knowledge_retrieval import fact_keys, keys, service


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def contract(
    *,
    intent: str = "answer_client",
    action: str = "answer_travel_question",
    slots: dict[str, Any] | None = None,
) -> ActionContract:
    return ActionContract(
        intent=intent,
        action=action,
        subject_ref_type=None,
        subject_ref_id=None,
        slots=slots or {"knowledge_domain": "travel_advantage"},
        source="text",
        confidence=0.95,
    )


class CountingKnowledgeService:
    def __init__(self, delegate: KnowledgeService) -> None:
        self.delegate = delegate
        self.calls: list[str] = []

    async def retrieve(self, question: str) -> KnowledgeBundle:
        self.calls.append(question)
        return await self.delegate.retrieve(question)


class FailingKnowledgeService:
    def __init__(self) -> None:
        self.calls = 0

    async def retrieve(self, question: str) -> KnowledgeBundle:
        self.calls += 1
        raise RuntimeError("knowledge database unavailable")


def resolver(tmp_path: Path) -> tuple[ReferenceResolver, CountingKnowledgeService]:
    counting = CountingKnowledgeService(service(tmp_path))
    return ReferenceResolver(counting), counting


def test_resolved_action_context_public_shape() -> None:
    fields = tuple(ResolvedActionContext.__dataclass_fields__)
    assert fields == (
        "action_contract", "knowledge_bundle", "need_knowledge",
        "needs_clarification", "requires_current_source",
        "clarification_options",
    )


def test_travel_advantage_and_points_comparison_retrieve_once(tmp_path: Path) -> None:
    reference, counting = resolver(tmp_path)
    action = contract()

    travel = run(reference.resolve(
        question="Что такое Travel Advantage?",
        action_contract=action,
        primary_module=Module.TRAVEL_ASSISTANT,
    ))
    assert travel.need_knowledge is True
    assert travel.knowledge_bundle is not None
    assert "ta.platform" in keys(travel.knowledge_bundle)
    assert len(counting.calls) == 1

    points = run(reference.resolve(
        question="Чем Loyalty Points отличаются от Travel Credits?",
        action_contract=action,
        primary_module=Module.TRAVEL_ASSISTANT,
    ))
    assert points.knowledge_bundle is not None
    assert {"ta.loyalty_points", "ta.travel_credits"} <= keys(points.knowledge_bundle)
    assert len(counting.calls) == 2  # exactly once for each independent turn


def test_silver_compensation_includes_grounding_and_compliance(tmp_path: Path) -> None:
    reference, counting = resolver(tmp_path)
    action = contract(
        action="explain_compensation",
        slots={
            "knowledge_domain": "mwr_life",
            "include_income_compliance": True,
        },
    )
    resolved = run(reference.resolve(
        question="Что получает Silver с Elite Turbo?",
        action_contract=action,
        primary_module=Module.TRAVEL_ASSISTANT,
    ))

    assert resolved.knowledge_bundle is not None
    assert {"ta.member_bonus", "ta.builder_bonus"} <= keys(resolved.knowledge_bundle)
    assert "mwr.compliance.no_specific_income_guarantee" in fact_keys(
        resolved.knowledge_bundle
    )
    assert resolved.knowledge_bundle.compliance_facts
    assert len(counting.calls) == 1


def test_ruby_ambiguity_becomes_stable_key_options(tmp_path: Path) -> None:
    reference, counting = resolver(tmp_path)
    resolved = run(reference.resolve(
        question="Какие выплаты у Ruby?",
        action_contract=contract(action="explain_compensation"),
        primary_module=Module.TRAVEL_ASSISTANT,
    ))

    assert resolved.need_knowledge is True
    assert resolved.needs_clarification is True
    assert resolved.knowledge_bundle is not None
    assert {"ta.dual_team_income", "ta.builder_bonus", "ta.rank_qualification"} <= set(
        resolved.clarification_options
    )
    assert all(isinstance(key, str) and key for key in resolved.clarification_options)
    assert len(counting.calls) == 1


def test_income_compliance_is_passed_through_unchanged(tmp_path: Path) -> None:
    reference, counting = resolver(tmp_path)
    resolved = run(reference.resolve(
        question="Сколько я гарантированно заработаю?",
        action_contract=contract(
            action="check_travel_claim",
            slots={"knowledge_domain": "mwr_life"},
        ),
        primary_module=Module.SAFETY_LAYER,
    ))

    assert resolved.knowledge_bundle is not None
    assert "mwr.compliance.no_specific_income_guarantee" in fact_keys(
        resolved.knowledge_bundle
    )
    assert resolved.knowledge_bundle.compliance_facts
    assert len(counting.calls) == 1


def test_current_life_experience_requires_current_source(tmp_path: Path) -> None:
    reference, counting = resolver(tmp_path)
    resolved = run(reference.resolve(
        question="Какой сейчас актуальный Life Experience?",
        action_contract=contract(
            slots={
                "knowledge_domain": "travel_advantage",
                "current_data_requested": True,
            }
        ),
        primary_module=Module.TRAVEL_ASSISTANT,
    ))

    assert resolved.knowledge_bundle is not None
    assert "ta.life_experiences" in keys(resolved.knowledge_bundle)
    assert "ta.compliance.current_information_check" in fact_keys(
        resolved.knowledge_bundle
    )
    assert resolved.requires_current_source is True
    assert len(counting.calls) == 1


@pytest.mark.parametrize(
    ("module", "intent", "action", "flow"),
    [
        (Module.CONTENT_FACTORY, "create_content", "generate_draft", "content_factory"),
        (Module.LEAD_RADAR, "find_signals", "show_signals", "radar"),
        (Module.ORCHESTRATOR, "other", "planner", "planner"),
        (Module.ORCHESTRATOR, "analyze_source", "analyze_source", "source_analysis"),
        (Module.ORCHESTRATOR, "other", "other", "generic"),
    ],
)
def test_ineligible_flows_never_call_knowledge(
    tmp_path: Path,
    module: Module,
    intent: str,
    action: str,
    flow: str,
) -> None:
    reference, counting = resolver(tmp_path)
    resolved = run(reference.resolve(
        question="Обычный запрос, не относящийся к Travel Advantage",
        action_contract=contract(intent=intent, action=action, slots={"flow": flow}),
        primary_module=module,
    ))

    assert resolved.need_knowledge is False
    assert resolved.knowledge_bundle is None
    assert resolved.needs_clarification is False
    assert resolved.requires_current_source is False
    assert resolved.clarification_options == ()
    assert counting.calls == []


def test_explicit_no_knowledge_gate_wins_for_eligible_module(tmp_path: Path) -> None:
    reference, counting = resolver(tmp_path)
    resolved = run(reference.resolve(
        question="Generic travel conversation",
        action_contract=contract(slots={"need_knowledge": False}),
        primary_module=Module.TRAVEL_ASSISTANT,
    ))
    assert resolved.need_knowledge is False
    assert counting.calls == []


def test_knowledge_exception_fails_closed_without_ungrounded_fallback() -> None:
    failing = FailingKnowledgeService()
    reference = ReferenceResolver(failing)
    action = contract()

    resolved = run(reference.resolve(
        question="Что такое Travel Advantage?",
        action_contract=action,
        primary_module=Module.TRAVEL_ASSISTANT,
    ))

    assert failing.calls == 1
    assert resolved.action_contract is action
    assert resolved.need_knowledge is True
    assert resolved.knowledge_bundle is None
    assert resolved.needs_clarification is True
    assert resolved.requires_current_source is False
    assert resolved.clarification_options == ()
