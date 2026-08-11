from __future__ import annotations

from app.domain.partners import UserConsent, WorkspaceContext
from app.repositories.partner_repository import PartnerRepository


PILOT_CONSENT_VERSION = "pilot-v1"


class PilotConsentRequiredError(PermissionError):
    """Paid-pilot access requires the current personal-data consent."""


class PilotConsentService:
    def __init__(self, repository: PartnerRepository) -> None:
        self.repository = repository

    async def accept_current(
        self, workspace_context: WorkspaceContext | None
    ) -> UserConsent:
        context = _active_context(workspace_context)
        return await self.repository.accept_user_consent(
            context.workspace_id,
            context.telegram_user_id,
            PILOT_CONSENT_VERSION,
        )

    async def has_current(
        self, workspace_context: WorkspaceContext | None
    ) -> bool:
        context = _active_context(workspace_context)
        return await self.repository.has_user_consent(
            context.workspace_id,
            context.telegram_user_id,
            PILOT_CONSENT_VERSION,
        )

    async def require_for_paid_access(
        self, workspace_context: WorkspaceContext | None
    ) -> None:
        if not await self.has_current(workspace_context):
            raise PilotConsentRequiredError(
                "Для платного доступа требуется актуальное согласие"
            )


def _active_context(context: WorkspaceContext | None) -> WorkspaceContext:
    if context is None or context.workspace_status != "active":
        raise PilotConsentRequiredError("Paid workspace недоступен")
    return context
