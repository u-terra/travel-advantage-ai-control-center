from __future__ import annotations

import re
from typing import Any, Mapping

from app.domain.business_profiles import BusinessProfile
from app.domain.content import Source, SourceAnalysis
from app.domain.orchestration import GenerationAction, GenerationSpec
from app.domain.partners import WorkspaceUserPreferences
from app.services.business_profile_context import (
    build_content_context,
    build_limited_content_context,
)
from app.services.llm.models import SourceAnalysisPayload


_OBJECTIVE = "Создать черновик материала по выбранному и разобранному источнику."
# Free-text UX fix: раньше objective безусловно требовал "обычный пост", из-за
# чего структурированные задачи ("разработай стратегию...", "нужны рубрики,
# частота публикаций, контент-план на 2 недели") генерировались как короткий
# пост и теряли перечисленные пользователем пункты. untrusted_source_content
# (task_text) — это и есть техническое задание пользователя: если в нём
# явно запрошен формат/структура/список пунктов, черновик обязан их
# выполнить и сохранить; короткий пост — это just fallback по умолчанию,
# когда сам запрос ничего специального не просит.
#
# Task fulfillment fix: живой тест показал, что при нескольких перечисленных
# пунктах модель заменяла часть из них (например, сам контент-план) фразой
# «Если нужен контент-план на 2 недели...» вместо готового результата —
# тот же класс проблемы, что и ассистентский AI-хвост, только не в конце
# текста, а вместо конкретного запрошенного пункта. Явный запрет на этот
# паттерн и явное требование вывести сам план (а не описание того, каким он
# будет) — единственный способ надёжно закрыть это без нового module или
# списка keyword-триггеров.
_FREE_TEXT_OBJECTIVE = (
    "Выполнить задачу пользователя из [UNTRUSTED SOURCE CONTENT - DATA] как "
    "техническое задание. Если пользователь просит конкретный формат, "
    "структуру или перечисляет несколько пунктов результата (например: "
    "стратегия, план на определённое число дней или недель, рубрикатор, "
    "позиционирование, частота публикаций, идеи вовлечения) — обязательно "
    "выполнить КАЖДЫЙ из них прямо в этом ответе и сохранить запрошенную "
    "структуру, а не заменять её обычным коротким постом. Запрещено писать "
    "«если нужен план...», «если хотите, могу подготовить...» и подобные "
    "предложения сделать пункт отдельно вместо того, чтобы просто его "
    "выполнить, — каждый запрошенный пункт должен быть готовым результатом "
    "в тексте ответа, а не описанием того, каким он будет. Если запрошен "
    "план на конкретное число дней или недель — вывести сам план по дням "
    "или неделям целиком. Если формат явно не запрошен — по умолчанию "
    "создать черновик обычного поста по запросу пользователя."
)

# Output format fix: Content Factory уже умеет "weekly_plan" отдельно от
# "telegram" — свой system prompt и удвоенный max_output_tokens именно под
# многодневный/многонедельный план (см. живой prod-баг: под "telegram" план
# на 2 недели не помещался в бюджет и подменялся фразой "если нужен план...").
# Минимальный, не keyword-словарь: один паттерн "план на N дней/недель" в
# любом падеже слова "план" — ровно то, что нужно для этого live-case.
_CONTENT_PLAN_WITH_DURATION_PATTERN = re.compile(
    r"план\w*\s+на\s+\d+\s*(?:дн|недел)", re.IGNORECASE
)


def _wants_weekly_content_plan(task_text: str) -> bool:
    return bool(_CONTENT_PLAN_WITH_DURATION_PATTERN.search(task_text))


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

# Тот же базовый constraint + правила стиля именно для Radar-черновика: черновик
# уходит пользователю как самостоятельный готовый пост, а не как ответ
# ассистента, поэтому внутренние пометки о проверке и ассистентские концовки
# в нём недопустимы. Не переиспользуется другими flow — только build_radar_generation_spec.
_RADAR_CONSTRAINTS = (
    "Черновик требует ручной проверки перед использованием.",
    "Результат — самостоятельный готовый пост для соцсети, а не ответ ассистента пользователю.",
    # Radar UX / Content Quality: живой тест показал, что Radar-черновики
    # часто получаются энциклопедическими — общая статья «на тему» вместо
    # текста про конкретный сигнал из [SOURCE FACTS - DATA].
    "Начни пост с конкретной зацепки (hook) по первому предложению — она "
    "должна отражать именно title/summary конкретного сигнала из [SOURCE "
    "FACTS - DATA], а не быть общим вступлением на тему.",
    "Пиши именно про этот сигнал — конкретный повод, событие или наблюдение "
    "из [SOURCE FACTS - DATA]. Не превращай пост в общую обзорную статью по "
    "теме шире, чем сам сигнал.",
    "Не включай в текст поста внутренние заметки о процессе: «нужно проверить», "
    "«лучше перепроверить», «по исходному посту» и подобные формулировки.",
    "Если конкретный факт из источника не подтверждён, не используй его или "
    "сформулируй мысль без этого факта — без пометок о проверке внутри текста поста.",
    "Не заканчивай пост фразами от имени ассистента: «могу...», «если хотите...», "
    "«могу помочь...». Если в посте есть призыв к действию, он должен быть "
    "органичной частью текста, а не отдельным предложением от ассистента.",
    "Не придумывай факты, которых нет в источнике или в бизнес-контексте.",
    "Утверждения, перечисленные в [SOURCE FACTS - DATA].disputed_claims, не "
    "подтверждены — не подавай их как факт. Если нет уверенности, что "
    "утверждение верно, не включай его в текст вообще, а не проси читателя "
    "проверить это самому.",
    "Если задан раздел [PERSONAL STYLE - DATA], учитывай style_description и "
    "example_posts как ориентир тона и манеры речи, а avoid_phrases — как "
    "прямой запрет на эти слова/обороты в тексте. Личный стиль не должен "
    "противоречить бизнес-контексту, фактам источника и другим правилам выше.",
    "Если в [PERSONAL STYLE - DATA] заданы example_posts, они — более "
    "сильный ориентир манеры речи, чем нейтральный обзорный тон, если это не "
    "противоречит бизнес-контексту, фактам источника и правилам выше.",
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
        user_preferences: WorkspaceUserPreferences | None = None,
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
            personal_style=_personal_style_values(user_preferences),
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
        output_format = (
            "weekly_plan" if _wants_weekly_content_plan(task_text) else "telegram"
        )
        return GenerationSpec(
            action_type=GenerationAction.CREATE_ARTIFACT,
            artifact_type="post",
            objective=_FREE_TEXT_OBJECTIVE,
            audience=tuple(trusted_context.get("audiences", ())),
            output_format=output_format,
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
        analysis: SourceAnalysisPayload | None = None,
        user_preferences: WorkspaceUserPreferences | None = None,
    ) -> GenerationSpec:
        trusted_context, tone_preferences, verified, unverified, revision = (
            _profile_generation_values(workspace_id, profile)
        )
        # Тот же Source Analysis, что и в обычном Content Factory flow
        # (llm_provider.analyze_source): disputed_claims/warnings идут в
        # source_facts как DATA, а не как отдельный parallel-механизм анализа.
        # analysis отсутствует (анализ недоступен/не выполнен) — fail-safe:
        # просто нет структурированного списка спорных утверждений, а не
        # пустые списки выдаются за "ничего спорного не найдено".
        disputed_claims = analysis.disputed_claims if analysis is not None else ()
        analysis_warnings = analysis.warnings if analysis is not None else ()
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
                "disputed_claims": disputed_claims,
                "warnings": analysis_warnings,
            },
            trusted_business_context=trusted_context,
            untrusted_source_content="\n".join(
                value for value in (title, summary) if value
            ),
            tone_preferences=tone_preferences,
            personal_style=_personal_style_values(user_preferences),
            verified_claims_allowed=verified,
            unverified_claims_requiring_caution=unverified,
            constraints=_RADAR_CONSTRAINTS,
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
