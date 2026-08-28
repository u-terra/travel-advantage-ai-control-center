from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.action_contract import ActionContract
from app.routing.modules import Module
from app.services.knowledge_service import KnowledgeBundle


class KnowledgeRetriever(Protocol):
    async def retrieve(self, question: str) -> KnowledgeBundle: ...


@dataclass(frozen=True)
class ResolvedActionContext:
    """Turn-local result of resolving references after triage.

    It is deliberately not persisted and does not replace ActionContract as
    the description of the selected action.  A missing bundle while
    ``need_knowledge`` and ``needs_clarification`` are both true is the
    fail-closed outcome: downstream generation must not guess an answer.
    """

    action_contract: ActionContract
    knowledge_bundle: KnowledgeBundle | None
    need_knowledge: bool
    needs_clarification: bool
    requires_current_source: bool
    clarification_options: tuple[str, ...]


_ELIGIBLE_MODULES = frozenset({
    Module.TRAVEL_ASSISTANT,
    Module.SAFETY_LAYER,
    Module.PARTNER_PACKAGING,
})
_EXCLUDED_CONTEXT_TOKENS = frozenset({
    "content_factory", "create_content", "lead_radar", "radar",
    "planner", "analyze_source", "source_analysis", "generic", "other",
})
_CURRENT_MARKERS = (
    "сейчас", "актуальн", "текущ", "сегодня", "availability", "current",
)


class ReferenceResolver:
    """Resolve optional KB grounding for an already-decided action.

    The resolver never chooses or changes ``primary_module``.  Eligibility
    is a deterministic post-triage gate; KnowledgeService remains the only
    component that performs knowledge retrieval.
    """

    def __init__(self, knowledge_service: KnowledgeRetriever) -> None:
        self._knowledge_service = knowledge_service

    async def resolve(
        self,
        *,
        question: str,
        action_contract: ActionContract,
        primary_module: Module,
    ) -> ResolvedActionContext:
        need_knowledge = self._needs_knowledge(action_contract, primary_module)
        if not need_knowledge:
            return ResolvedActionContext(
                action_contract=action_contract,
                knowledge_bundle=None,
                need_knowledge=False,
                needs_clarification=False,
                requires_current_source=False,
                clarification_options=(),
            )

        try:
            bundle = await self._knowledge_service.retrieve(
                _retrieval_question(question, action_contract)
            )
        except Exception:
            # Knowledge was required, therefore silently continuing without
            # grounding would be unsafe.  No error detail or invented option
            # leaks into the conversation contract.
            return ResolvedActionContext(
                action_contract=action_contract,
                knowledge_bundle=None,
                need_knowledge=True,
                needs_clarification=True,
                requires_current_source=False,
                clarification_options=(),
            )

        needs_clarification = bundle.potentially_ambiguous
        options = (
            tuple(dict.fromkeys(item.stable_key for item in bundle.primary_items))
            if needs_clarification else ()
        )
        return ResolvedActionContext(
            action_contract=action_contract,
            knowledge_bundle=bundle,
            need_knowledge=True,
            needs_clarification=needs_clarification,
            requires_current_source=_requires_current_source(question, action_contract, bundle),
            clarification_options=options,
        )

    @staticmethod
    def _needs_knowledge(
        action_contract: ActionContract, primary_module: Module,
    ) -> bool:
        if primary_module not in _ELIGIBLE_MODULES:
            return False
        tokens = {
            action_contract.intent.casefold(),
            action_contract.action.casefold(),
            str(action_contract.slots.get("flow", "")).casefold(),
        }
        if tokens.intersection(_EXCLUDED_CONTEXT_TOKENS):
            return False
        explicit = action_contract.slots.get("need_knowledge")
        if explicit is False:
            return False
        if primary_module is Module.TRAVEL_ASSISTANT:
            return True
        domain = str(action_contract.slots.get("knowledge_domain", "")).casefold()
        return explicit is True or domain in {"travel_advantage", "mwr_life"}


def _retrieval_question(question: str, action_contract: ActionContract) -> str:
    """Add a retrieval-only safety hint supplied by the decided contract.

    This does not reinterpret facts or change routing.  It lets an upstream
    triage/action producer request the already-existing income-compliance
    block in the same (single) KnowledgeService call as compensation facts.
    """
    if action_contract.slots.get("include_income_compliance") is True:
        return f"{question}\nПроверить гарантированный доход."
    return question


def _requires_current_source(
    question: str,
    action_contract: ActionContract,
    bundle: KnowledgeBundle,
) -> bool:
    requested = action_contract.slots.get("current_data_requested") is True
    lowered = question.casefold()
    requested = requested or any(marker in lowered for marker in _CURRENT_MARKERS)
    if not requested:
        return False
    return any(
        fact.fact_type == "staleness_compliance_rule"
        for fact in bundle.compliance_facts
    )
