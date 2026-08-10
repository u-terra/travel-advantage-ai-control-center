from __future__ import annotations

from typing import Any

from app.domain.business_profiles import BusinessClaim, BusinessProfile
from app.domain.partners import WorkspaceContext
from app.repositories.partner_repository import PartnerRepository


class BusinessProfileAccessError(PermissionError):
    pass


class BusinessProfileService:
    def __init__(self, repository: PartnerRepository) -> None:
        self.repository = repository

    async def get(self, workspace_context: WorkspaceContext | None) -> BusinessProfile | None:
        _require_read(workspace_context)
        return await self.repository.get_business_profile(workspace_context.workspace_id)

    async def create(self, workspace_context: WorkspaceContext | None, **values: Any) -> BusinessProfile:
        _require_write(workspace_context)
        return await self.repository.create_business_profile(workspace_context.workspace_id, **values)

    async def update(
        self, workspace_context: WorkspaceContext | None, expected_revision: int, **values: Any
    ) -> BusinessProfile:
        _require_write(workspace_context)
        return await self.repository.update_business_profile(
            workspace_context.workspace_id, expected_revision, **values
        )


def build_content_context(profile: BusinessProfile) -> dict[str, Any]:
    context = profile.context
    return {
        "business_name": profile.business_name,
        "business_type": profile.business_type,
        "short_description": profile.short_description,
        "specializations": list(context.specializations),
        "destinations": list(context.destinations),
        "audiences": list(context.audiences),
        "markets": list(context.markets),
        "positioning": _projection_mapping(context.positioning),
        "communication": _projection_mapping(context.communication),
        "goals": list(context.goals),
        "content_preferences": _projection_mapping(context.content_preferences),
        "public_contacts": dict(context.public_contacts),
        "claims": [_claim_projection(claim) for claim in context.claims],
    }


def build_competitor_context(profile: BusinessProfile) -> dict[str, Any]:
    context = profile.context
    return {
        "business_name": profile.business_name,
        "business_type": profile.business_type,
        "short_description": profile.short_description,
        "specializations": list(context.specializations),
        "markets": list(context.markets),
        "positioning": _projection_mapping(context.positioning),
        "claims": [_claim_projection(claim) for claim in context.claims],
    }


def build_assistant_context(profile: BusinessProfile) -> dict[str, Any]:
    context = profile.context
    communication = context.communication
    return {
        "business_name": profile.business_name,
        "short_description": profile.short_description,
        "specializations": list(context.specializations),
        "destinations": list(context.destinations),
        "markets": list(context.markets),
        "public_contacts": dict(context.public_contacts),
        "preferred_terms": list(communication["preferred_terms"]),
        # Safer boundary: unverified claims are excluded entirely.
        "verified_claims": [
            _claim_projection(claim)
            for claim in context.claims
            if claim.verification_status == "verified"
        ],
    }


def _claim_projection(claim: BusinessClaim) -> dict[str, Any]:
    return {
        "text": claim.text,
        "verification_status": claim.verification_status,
        "evidence_reference": claim.evidence_reference,
    }


def _projection_mapping(value) -> dict[str, Any]:
    return {
        key: list(item) if isinstance(item, tuple) else item
        for key, item in value.items()
    }


def _require_read(context: WorkspaceContext | None) -> None:
    if context is None or context.workspace_status != "active" or context.role not in {"owner", "admin", "member"}:
        raise BusinessProfileAccessError("Business Profile недоступен")


def _require_write(context: WorkspaceContext | None) -> None:
    if context is None or context.workspace_status != "active" or context.role not in {"owner", "admin"}:
        raise BusinessProfileAccessError("Недостаточно прав для изменения Business Profile")
