"""F2A: shared "record real progress into Working State" helper.

Introduced because F2A wires the *same* bookkeeping ("this flow just
produced/updated an Artifact, so remember active_module/current_task/
current_artifact_id/last_action") into five different call sites
(material_generation, text_review x2, the Radar content-idea draft in
menu.py, and the client-reply flow in reply_sync.py). Without this, the
same patch_state + try/except/log dance would be copy-pasted five times.

This is deliberately thin - not a ConversationManager/God Object. It knows
nothing about Telegram, artifacts, competitors, or any business rule; it
only knows how to fail closed on a technical write error without breaking
a flow that already produced real user-facing content (see the F2A report,
section H: error policy).

Tenant isolation is NOT enforced here - it is enforced by every call site
already validating workspace_context before ever reaching this service.
Swallowing exceptions here is only ever about *this* write failing, never
about masking a missing/invalid tenant.
"""

from __future__ import annotations

import logging

from app.repositories.conversation_state_repository import ConversationStateRepository

log = logging.getLogger(__name__)


class ConversationStateService:
    def __init__(self, repository: ConversationStateRepository | None) -> None:
        self._repository = repository

    async def record_artifact(
        self,
        workspace_id: int,
        telegram_user_id: int,
        artifact_id: int,
        *,
        active_module: str,
        current_task: str,
        last_action: str,
    ) -> None:
        """Best-effort: a failure here must never turn a successful
        generation/save into a user-visible error (see F2A report, "must
        not lose already-created content / must not turn success into
        routing failed")."""
        if self._repository is None:
            return
        try:
            await self._repository.patch_state(
                workspace_id, telegram_user_id,
                active_module=active_module,
                current_task=current_task,
                current_artifact_id=artifact_id,
                last_action=last_action,
            )
        except Exception:
            log.warning("conversation_state: failed to record artifact progress", exc_info=True)

    async def record_subject_ref(
        self,
        workspace_id: int,
        telegram_user_id: int,
        *,
        subject_ref_type: str,
        subject_ref_id: int,
        active_module: str,
        current_task: str,
        last_action: str,
    ) -> None:
        """Same best-effort contract as record_artifact, for the "user
        opened/acted on a known existing object" case (e.g. a saved
        competitor) rather than an artifact."""
        if self._repository is None:
            return
        try:
            await self._repository.patch_state(
                workspace_id, telegram_user_id,
                active_module=active_module,
                current_task=current_task,
                current_subject_ref_type=subject_ref_type,
                current_subject_ref_id=subject_ref_id,
                last_action=last_action,
            )
        except Exception:
            log.warning("conversation_state: failed to record subject ref", exc_info=True)
