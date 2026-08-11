from __future__ import annotations

from typing import Any, Mapping

from app.domain.business_profiles import BusinessProfile
from app.domain.content import Source, SourceAnalysis
from app.domain.orchestration import GenerationAction, GenerationSpec
from app.services.business_profile_context import (
    build_content_context,
    build_limited_content_context,
)


_OBJECTIVE = "Создать черновик материала по выбранному и разобранному источнику."
_FREE_TEXT_OBJECTIVE = "Создать черновик обычного поста по запросу пользователя."
_RADAR_OBJECTIVE = "Создать черновик информационного материала по выбранному Radar-сигналу."
_CONSTRAINTS = ("Черновик требует ручной проверки перед использованием.",)


class MaterialOrchestrationService:
    """Build a provider-neutral spec from inputs authorized by the caller."""

    def build_generation_spec(
        self,
        workspace_id: int,
        source: Source,
        analysis: SourceAnalysis,
        profile: BusinessProfile | None,
        *,
        artifact_type: str,
        output_format: str,
    ) -> GenerationSpec:
        if source.workspace_id != workspace_id or analysis.workspace_id != workspace_id:
            raise PermissionError("Source и SourceAnalysis не принадлежат workspace")
        if analysis.source_id != source.id:
            raise ValueError("SourceAnalysis не соответствует Source")
        trusted_context, tone_preferences, verified, unverified, revision = (
            _profile_generation_values(workspace_id, profile)
        )

        return GenerationSpec(
            action_type=GenerationAction.CREATE_ARTIFACT,
            artifact_type=artifact_type,
            objective=_OBJECTIVE,
            audience=tuple(dict.fromkeys(
                [*trusted_context.get("audiences", ()), *analysis.target_audiences]
            )),
            output_format=output_format,
            source_facts={
                "summary": analysis.summary,
                "key_facts": analysis.key_facts,
                "audience_value": analysis.audience_value,
                "content_angles": analysis.content_angles,
                "recommended_formats": analysis.recommended_formats,
                "disputed_claims": analysis.disputed_claims,
                "warnings": analysis.warnings,
            },
            trusted_business_context=trusted_context,
            untrusted_source_content=source.original_text or "",
            tone_preferences=tone_preferences,
            verified_claims_allowed=tuple(verified),
            unverified_claims_requiring_caution=tuple(unverified),
            constraints=_CONSTRAINTS,
            profile_revision_used=revision,
        )

    def build_free_text_generation_spec(
        self,
        workspace_id: int,
        task_text: str,
        profile: BusinessProfile | None,
    ) -> GenerationSpec:
        if not isinstance(task_text, str) or not task_text.strip():
            raise ValueError("task_text не должен быть пустым")
        trusted_context, tone_preferences, verified, unverified, revision = (
            _profile_generation_values(workspace_id, profile)
        )
        return GenerationSpec(
            action_type=GenerationAction.CREATE_ARTIFACT,
            artifact_type="post",
            objective=_FREE_TEXT_OBJECTIVE,
            audience=tuple(trusted_context.get("audiences", ())),
            output_format="telegram",
            source_facts={},
            trusted_business_context=trusted_context,
            untrusted_source_content=task_text,
            tone_preferences=tone_preferences,
            verified_claims_allowed=verified,
            unverified_claims_requiring_caution=unverified,
            constraints=_CONSTRAINTS,
            profile_revision_used=revision,
        )

    def build_radar_generation_spec(
        self,
        workspace_id: int,
        profile: BusinessProfile | None,
        *,
        title: str,
        summary: str,
        source_type: str,
        origin_type: str,
        url: str,
        category: str,
        reason: str,
    ) -> GenerationSpec:
        trusted_context, tone_preferences, verified, unverified, revision = (
            _profile_generation_values(workspace_id, profile)
        )
        return GenerationSpec(
            action_type=GenerationAction.CREATE_ARTIFACT,
            artifact_type="post",
            objective=_RADAR_OBJECTIVE,
            audience=tuple(trusted_context.get("audiences", ())),
            output_format="telegram",
            source_facts={
                "title": title,
                "summary": summary,
                "source_type": source_type,
                "origin_type": origin_type,
                "url": url,
                "category": category,
                "reason": reason,
            },
            trusted_business_context=trusted_context,
            untrusted_source_content="\n".join(
                value for value in (title, summary) if value
            ),
            tone_preferences=tone_preferences,
            verified_claims_allowed=verified,
            unverified_claims_requiring_caution=unverified,
            constraints=_CONSTRAINTS,
            profile_revision_used=revision,
        )


def _profile_generation_values(
    workspace_id: int, profile: BusinessProfile | None,
) -> tuple[
    dict[str, Any], dict[str, Any], tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...], int | None,
]:
    if profile is not None and profile.workspace_id != workspace_id:
        raise PermissionError("Business Profile не принадлежит workspace")
    if profile is not None and profile.profile_status not in {"usable", "incomplete"}:
        raise ValueError("Неизвестный status Business Profile")

    trusted_context: dict[str, Any] = {}
    tone_preferences: dict[str, Any] = {}
    verified: list[Mapping[str, Any]] = []
    unverified: list[Mapping[str, Any]] = []
    revision = None
    if profile is not None:
        projection = (
            build_content_context(profile)
            if profile.profile_status == "usable"
            else build_limited_content_context(profile)
        )
        trusted_context = dict(projection)
        trusted_context.pop("claims", None)
        tone_preferences = dict(trusted_context.pop("communication", {}))
        if profile.profile_status == "usable":
            tone_preferences.update(trusted_context.pop("content_preferences", {}))
        for claim in profile.context.claims:
            value = {
                "text": claim.text,
                "verification_status": claim.verification_status,
                "evidence_reference": claim.evidence_reference,
            }
            (verified if claim.verification_status == "verified" else unverified).append(value)
        revision = profile.revision
    return (
        trusted_context, tone_preferences, tuple(verified), tuple(unverified), revision,
    )
