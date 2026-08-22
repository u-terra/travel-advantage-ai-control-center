from __future__ import annotations

from typing import Any, Mapping

from app.domain.business_profiles import BusinessProfile
from app.domain.content import Source, SourceAnalysis
from app.domain.orchestration import GenerationAction, GenerationSpec
from app.domain.partners import WorkspaceUserPreferences
from app.services.business_profile_context import (
    build_content_context,
    build_limited_content_context,
)


_OBJECTIVE = "Создать черновик материала по выбранному и разобранному источнику."
_FREE_TEXT_OBJECTIVE = "Создать черновик обычного поста по запросу пользователя."
_RADAR_OBJECTIVE = "Создать черновик информационного материала по выбранному Radar-сигналу."
_CONSTRAINTS = (
    "Черновик требует ручной проверки перед использованием.",
    # UX polish: живой тест Stage 3B1 показал типичные AI-хвосты в готовом
    # посте («если хотите, могу сравнить...») — они не читаются как текст
    # автора поста. Запрет на конкретные ассистентские фразы, а не на CTA
    # вообще: естественный призыв к действию (забронировать, написать,
    # перейти по ссылке) по-прежнему допустим.
    "Не завершай текст служебными фразами от имени ассистента: «если "
    "хотите, могу...», «могу помочь...», «напишите — разберу...», «могу "
    "сравнить варианты...» и аналогичными репликами AI, если пользователь "
    "явно не попросил такой CTA. Результат должен читаться как "
    "самостоятельный текст автора поста, а не как ответ ассистента. Обычный "
    "естественный для поста CTA (например, забронировать, написать в "
    "директ, перейти по ссылке) не запрещён.",
    # Stage 3B1: приоритет источников стиля — эти правила (безопасность,
    # бизнес-факты) всегда выше личного стиля пользователя. [PERSONAL STYLE
    # - DATA] влияет только на тон/формулировки и содержит avoid_phrases —
    # список слов/оборотов, которых явно нужно избегать.
    "Если задан раздел [PERSONAL STYLE - DATA], учитывай style_description и "
    "example_posts как ориентир тона и манеры речи, а avoid_phrases — как "
    "прямой запрет на эти слова/обороты в тексте. Личный стиль не должен "
    "противоречить бизнес-контексту, фактам источника и другим правилам выше.",
    "Если в [PERSONAL STYLE - DATA] заданы example_posts, они — более "
    "сильный ориентир манеры речи, чем общий тон бренда, если это не "
    "противоречит бизнес-контексту, фактам источника и правилам выше.",
)

_CLIENT_REPLY_OBJECTIVE = "Сформировать короткий личный ответ клиенту в Telegram по его вопросу."

# Stage 3B1: TRAVEL_ASSISTANT (client reply) больше не обходит structured
# orchestration через сырой source_text — та же логика приоритета источников
# стиля, что и в _CONSTRAINTS выше, плюс сохранённая формулировка прежнего
# ручного prompt'а из app/handlers/tasks.py (_draft_request_for).
_CLIENT_REPLY_CONSTRAINTS = (
    "Черновик требует ручной проверки перед отправкой.",
    "Ответь простыми словами и по существу. Не обещай доход, окупаемость или "
    "гарантированные скидки. Не утверждай, что формат подходит всем. Не "
    "используй фразу «без давления». Если точных данных недостаточно, не "
    "выдумывай: предложи уточнить детали или спокойно разобрать вопрос лично.",
    # UX polish: живой тест Stage 3B1 показал типичный AI-хвост в ответе
    # клиенту («если хотите, можно сравнить... напишите — разберу»). Это
    # сообщение живого человека клиенту, а не реплика ассистента — обычное
    # предложение следующего шага уместно, но не в виде универсального
    # AI-хвоста.
    "Не завершай ответ служебными фразами от имени ассистента: «если "
    "хотите, могу...», «могу помочь...», «напишите — разберу...», «могу "
    "сравнить варианты...» и аналогичными репликами AI. Это сообщение "
    "живого человека клиенту, а не ответ ассистента: естественное "
    "предложение следующего шага уместно (например, «уточню детали и "
    "напишу точнее» или «скажите даты — посмотрю варианты»), но оно должно "
    "звучать как реплика самого пользователя, а не как универсальный "
    "AI-хвост.",
    "Если задан раздел [PERSONAL STYLE - DATA], учитывай style_description и "
    "example_posts как ориентир тона и манеры речи, а avoid_phrases — как "
    "прямой запрет на эти слова/обороты в тексте. Личный стиль не должен "
    "противоречить бизнес-контексту, verified/unverified claims и другим "
    "правилам выше.",
    "Если в [PERSONAL STYLE - DATA] заданы example_posts, они — более "
    "сильный ориентир манеры речи, чем общий тон бренда, если это не "
    "противоречит бизнес-контексту, verified/unverified claims и правилам "
    "выше.",
)

# Добавляется к _CLIENT_REPLY_CONSTRAINTS только когда decision.safety_level
# не NOT_REQUIRED — прежнее поведение safety_instruction в _draft_request_for.
_CLIENT_REPLY_SAFETY_CONSTRAINT = (
    "Это вопрос с обязательной Safety-проверкой. Не сообщай цены, тарифы, "
    "доступность, способы оплаты, варианты бронирования или сравнения как "
    "установленный факт. Дай только общее объяснение и прямо укажи, что "
    "конкретные условия нужно сверить вручную."
)


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
        *,
        user_preferences: WorkspaceUserPreferences | None = None,
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
            personal_style=_personal_style_values(user_preferences),
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

    def build_client_reply_generation_spec(
        self,
        workspace_id: int,
        client_question: str,
        profile: BusinessProfile | None,
        *,
        safety_required: bool,
        user_preferences: WorkspaceUserPreferences | None = None,
    ) -> GenerationSpec:
        """Stage 3B1: заменяет прежний прямой вызов provider.generate_draft()
        для TRAVEL_ASSISTANT (client reply) в app/handlers/tasks.py — тот путь
        полностью обходил structured orchestration и personal_style. Здесь
        используется тот же BusinessProfile workspace, verified/unverified
        claims и приоритет источников стиля, что и в остальных build_*
        методах этого сервиса.
        """
        if not isinstance(client_question, str) or not client_question.strip():
            raise ValueError("client_question не должен быть пустым")
        trusted_context, tone_preferences, verified, unverified, revision = (
            _profile_generation_values(workspace_id, profile)
        )
        constraints = _CLIENT_REPLY_CONSTRAINTS
        if safety_required:
            constraints = (*constraints, _CLIENT_REPLY_SAFETY_CONSTRAINT)
        return GenerationSpec(
            action_type=GenerationAction.CREATE_ARTIFACT,
            artifact_type="client_message",
            objective=_CLIENT_REPLY_OBJECTIVE,
            audience=tuple(trusted_context.get("audiences", ())),
            output_format="telegram",
            source_facts={},
            trusted_business_context=trusted_context,
            untrusted_source_content=client_question,
            tone_preferences=tone_preferences,
            personal_style=_personal_style_values(user_preferences),
            verified_claims_allowed=verified,
            unverified_claims_requiring_caution=unverified,
            constraints=constraints,
            profile_revision_used=revision,
        )


def _personal_style_values(
    user_preferences: WorkspaceUserPreferences | None,
) -> dict[str, Any]:
    """Личный стиль КОНКРЕТНОГО пользователя — отдельная DATA-секция от
    trusted_business_context (стиль компании). Пусто, если пользователь
    ничего не заполнил — тогда генерация просто не получает эту секцию,
    не заменяет её выдуманными значениями.
    """
    if user_preferences is None:
        return {}
    values: dict[str, Any] = {}
    if user_preferences.style_description.strip():
        values["style_description"] = user_preferences.style_description
    if user_preferences.example_posts:
        values["example_posts"] = list(user_preferences.example_posts)
    if user_preferences.avoid_phrases:
        values["avoid_phrases"] = list(user_preferences.avoid_phrases)
    return values


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
        # Standard generation does not need direct contact details. Explicit
        # contact/CTA flows may opt in separately when product semantics exist.
        trusted_context.pop("public_contacts", None)
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
