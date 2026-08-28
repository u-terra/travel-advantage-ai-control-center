"""F2D: propose_content_topics -> PendingOffer(content_topics).

Deliberately NOT a method on MaterialOrchestrationService: GenerationSpec's
fields (artifact_type/output_format/trusted_business_context/...) describe
generating a single Artifact from a spec - that semantics does not fit
"propose N short topic ideas up front, generate nothing yet". Forcing this
into GenerationSpec would be exactly the kind of artificial reuse the F2D
spec warned against. A small standalone service is fewer new concepts than
teaching MaterialOrchestrationService an unrelated operation.

No handler calls this yet (F2D ships capability only, not a trigger from
free text - see the F2D report, "НЕ ДЕЛАТЬ ТРИГГЕР"). Same "infrastructure
ahead of producers" precedent as ActionContract (F1) and
ConversationArtifactService (F2B).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.domain.conversation_state import OfferItem, PendingOffer
from app.repositories.conversation_state_repository import (
    ConversationStateConflictError,
    ConversationStateRepository,
)
from app.services.llm.base import LLMProvider

log = logging.getLogger(__name__)

CONTENT_TOPICS_OFFER_TYPE = "content_topics"

# Same order of magnitude as the Radar content-idea offer TTL
# (app.handlers.menu._CONVERSATION_TTL) - a short-lived "pick one of a few
# options" interaction, not a long-running task.
_OFFER_TTL = timedelta(minutes=30)


def _expiry() -> str:
    return (datetime.now(timezone.utc) + _OFFER_TTL).isoformat()


class ContentTopicsService:
    def __init__(
        self,
        llm_provider: LLMProvider,
        conversation_state_repository: ConversationStateRepository,
    ) -> None:
        self._llm_provider = llm_provider
        self._conversation_state = conversation_state_repository

    async def propose_and_offer(
        self,
        workspace_id: int,
        telegram_user_id: int,
        *,
        source_text: str,
        count: int = 3,
    ) -> PendingOffer | None:
        """Exactly one provider call -> exactly one PendingOffer.

        Returns None if the provider call failed (network/timeout/invalid
        external response - see propose_topics_sync) or the offer could not
        be persisted. Never raises ConversationStateConflictError to the
        caller: see _replace_existing_offer for why that should not even be
        reachable in the normal case.
        """
        result = await asyncio.to_thread(
            self._llm_provider.propose_content_topics,
            source_text=source_text, count=count,
        )
        if result is None:
            return None

        items = tuple(
            OfferItem(
                id=topic.id, label=topic.title,
                # Only the small fields needed to render/select a topic -
                # no large text copied into payload (F2D constraint).
                payload={"angle": topic.angle, "reason": topic.reason},
            )
            for topic in result.topics
        )

        await self._replace_existing_offer(workspace_id, telegram_user_id)
        try:
            return await self._conversation_state.create_offer(
                workspace_id, telegram_user_id, CONTENT_TOPICS_OFFER_TYPE, items,
                expires_at=_expiry(),
            )
        except ConversationStateConflictError:
            # Only reachable on a genuine race (another call created a new
            # content_topics offer between _replace_existing_offer and this
            # create_offer) - a controlled "try again" outcome, not a crash,
            # and it never touches an unrelated offer_type (e.g. Radar).
            log.info(
                "content_topics: active offer reappeared concurrently, not overwritten"
            )
            return None

    async def resolve_offer_item(
        self,
        workspace_id: int,
        telegram_user_id: int,
        offer_id: int,
        item_id: str,
    ) -> OfferItem | None:
        """Selection service foundation (F2D section 10) - deterministic,
        no LLM, no guessing. Returns None (never raises) if there is no
        active content_topics offer for this workspace/user, the active
        offer's id does not match ``offer_id``, or no item in it has
        exactly ``item_id`` - all indistinguishable to the caller, same
        convention as ConversationStateRepository.consume_offer/
        answer_question. F2D does NOT call this from anywhere yet (no
        generation-from-selected-topic here - that is Reference Resolver
        territory, out of scope per the F2D report).
        """
        offer = await self._conversation_state.get_active_offer(
            workspace_id, telegram_user_id, CONTENT_TOPICS_OFFER_TYPE,
        )
        if offer is None or offer.id != offer_id:
            return None
        for item in offer.items:
            if item.id == item_id:
                return item
        return None

    async def _replace_existing_offer(
        self, workspace_id: int, telegram_user_id: int,
    ) -> None:
        """F2D offer-lifecycle decision (report section G): an explicit new
        topics request replaces the previous content_topics offer for the
        SAME (workspace, user) - never a generic multi-offer system, and
        never anything but content_topics (a Radar offer for the same user
        is a different offer_type/index key and is never touched here).
        """
        existing = await self._conversation_state.get_active_offer(
            workspace_id, telegram_user_id, CONTENT_TOPICS_OFFER_TYPE,
        )
        if existing is not None:
            await self._conversation_state.consume_offer(
                workspace_id, telegram_user_id, existing.id,
            )
