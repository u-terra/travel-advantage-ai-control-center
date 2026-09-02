from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import markdown
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.chat_provider import ChatConfig, OpenAIChatProvider
from app.config import load_settings
from app.domain.business_profiles import (
    BusinessProfileValidationError,
    StaleBusinessProfileError,
)
from app.domain.competitor_discovery import canonical_domain
from app.domain.usage import UsageStatus
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.knowledge_repository import KnowledgeRepository
from app.repositories.partner_repository import (
    PartnerRepository,
    TooManyUserExamplesError,
    business_context_to_dict,
)
from app.repositories.usage_ledger_repository import UsageLedgerRepository
from app.repositories.workspace_memory_repository import WorkspaceMemoryRepository
from app.repositories.workspace_signal_repository import WorkspaceSignalRepository
from app.services.business_profile_context import (
    BusinessProfileAccessError,
    BusinessProfileService,
    build_assistant_context,
)
from app.services.competitor_intelligence import (
    CompetitorIntelligenceService,
    CompetitorIntelligenceUnavailable,
)
from app.services.content_factory import ContentFactoryConfig
from app.services.knowledge_service import KnowledgeBundle, KnowledgeService
from app.services.lead_radar import LeadRadarConfig, build_workspace_signals, category_label
from app.services.llm.factory import create_llm_provider
from app.services.usage_recorder import record_llm_call


app = FastAPI(title="Travel AI Orchestrator Web API")

settings = load_settings()

chat_provider = OpenAIChatProvider(
    ChatConfig(
        api_key=settings.planner_openai_api_key,
        model="gpt-5.6-terra",
        timeout_seconds=180,
    )
)

knowledge_repository = KnowledgeRepository()
usage_ledger_repository = UsageLedgerRepository(settings.journal_db_path)
competitor_repository = CompetitorRepository(settings.journal_db_path)
partner_repository = PartnerRepository(settings.journal_db_path)
workspace_memory_repository = WorkspaceMemoryRepository(settings.journal_db_path)
artifact_repository = ArtifactRepository(settings.journal_db_path)
workspace_signal_repository = WorkspaceSignalRepository(
    settings.journal_db_path, settings.lead_radar_db_path
)
lead_radar_config = LeadRadarConfig(db_path=settings.lead_radar_db_path)

# Temporary until web authentication is implemented.
WEB_WORKSPACE_ID = 1
WEB_TELEGRAM_USER_ID = 586249067

# Верхняя граница объёма workspace memory, передаваемого в промпт.
MAX_WORKSPACE_MEMORY_CHARS = 6000

knowledge_service = KnowledgeService(
    knowledge_repository,
    max_primary_items=5,
    max_related_items=8,
    max_facts=30,
    max_examples=2,
)


content_factory_config = ContentFactoryConfig(
    url=settings.content_factory_url,
    token=settings.content_factory_token,
    timeout_seconds=settings.content_factory_timeout_seconds,
    source_analysis_url=settings.content_factory_source_analysis_url,
    topics_url=settings.content_factory_topics_url,
)

competitor_llm_provider = create_llm_provider(
    settings.llm_provider,
    content_factory_config=content_factory_config,
)

competitor_intelligence_service = CompetitorIntelligenceService(
    competitor_llm_provider,
    knowledge_service,
    usage_ledger_repository=usage_ledger_repository,
)


class ChatRequest(BaseModel):
    message: str
    history: list[dict[str, str]] = Field(default_factory=list)


@app.on_event("startup")
async def startup() -> None:
    await knowledge_repository.init()
    await usage_ledger_repository.init()
    await competitor_repository.init()
    await partner_repository.init()
    await workspace_memory_repository.init()
    await artifact_repository.init()
    # legacy_owner_workspace_id=None: the one-time legacy-Radar backfill is
    # already owned by the bot process (app/main.py) against the same shared
    # journal DB - this just ensures the schema exists, it never re-runs
    # that backfill from the web process.
    await workspace_signal_repository.init(None)


def _fact_value(fact) -> str:
    if fact.value_text is not None:
        return fact.value_text

    if fact.value_number is not None:
        value = str(fact.value_number)
    elif fact.range_min is not None or fact.range_max is not None:
        value = f"{fact.range_min}–{fact.range_max}"
    else:
        value = "(значение не указано)"

    suffix = " ".join(
        str(value)
        for value in (fact.currency, fact.unit, fact.period)
        if value
    )

    return f"{value} {suffix}".strip()


def _knowledge_context(bundle: KnowledgeBundle) -> str:
    if not (
        bundle.primary_items
        or bundle.facts
        or bundle.compliance_facts
    ):
        return ""

    lines = [
        "=== ПРОВЕРЕННАЯ БАЗА ЗНАНИЙ ===",
    ]

    if bundle.primary_items:
        lines.append("\nРелевантные разделы:")

        for item in bundle.primary_items[:5]:
            content = item.content.strip()

            if len(content) > 1200:
                content = content[:1200] + "…"

            lines.append(
                f"- [{item.stable_key}] {item.title}: {content} "
                f"(источник: {item.source_ref})"
            )

    if bundle.facts:
        lines.append("\nКанонические факты:")

        for fact in bundle.facts[:30]:
            condition = (
                f"; условие: {fact.condition_text}"
                if fact.condition_text
                else ""
            )

            lines.append(
                f"- [{fact.stable_key}] "
                f"{fact.subject_key}: {_fact_value(fact)}"
                f"{condition} "
                f"(источник: {fact.source_ref})"
            )

    if bundle.compliance_facts:
        lines.append("\nОбязательные ограничения:")

        for fact in bundle.compliance_facts:
            lines.append(
                f"- [{fact.stable_key}] {_fact_value(fact)} "
                f"(источник: {fact.source_ref})"
            )

    if bundle.examples:
        lines.append("\nПроверенные примеры:")

        for example in bundle.examples[:2]:
            lines.append(
                f"- {example.title}: {example.scenario}. "
                f"{example.explanation}"
            )

    if bundle.sources:
        lines.append("\nИсточники:")

        for source in bundle.sources[:8]:
            lines.append(
                f"- {source.title} | "
                f"{source.verification_status} | "
                f"{source.source_reference}"
            )

    if bundle.potentially_ambiguous:
        lines.append(
            "\nВнимание: запрос потенциально неоднозначен."
        )

        for reason in bundle.ambiguity_reasons:
            lines.append(f"- {reason}")

    for missing in bundle.missing_definitions:
        lines.append(f"- В базе отсутствует: {missing}")

    return "\n".join(lines)


async def _requested_competitor(message: str):
    lowered = message.lower()

    markers = (
        "проанализ",
        "анализ",
        "аудит",
        "разбери",
        "сравни",
        "конкурент",
    )

    if not any(marker in lowered for marker in markers):
        return None

    competitors = await competitor_repository.list_for_workspace(
        WEB_WORKSPACE_ID,
        limit=50,
    )

    for competitor in competitors:
        domain = canonical_domain(competitor.url).lower()
        label = competitor.label.lower()

        if domain and domain in lowered:
            return competitor

        if label and label in lowered:
            return competitor

    return None


def _competitor_context(intelligence) -> str:
    payload = json.dumps(
        asdict(intelligence),
        ensure_ascii=False,
        indent=2,
    )

    if len(payload) > 24000:
        payload = payload[:24000] + "\n... [контекст сокращён]"

    return (
        "=== COMPETITOR INTELLIGENCE ===\n"
        "Данные получены из публичных источников конкурента.\n"
        + payload
    )


def _business_profile_context(profile) -> str:
    """Same safe projection Content Factory uses for LLM prompts
    (build_assistant_context: unverified claims excluded) - reused as-is
    so editing BusinessProfile in /api/profile takes effect on the very
    next /api/chat call, without a second profile-context builder."""
    data = build_assistant_context(profile)
    lines = ["=== ПРОФИЛЬ БИЗНЕСА ==="]

    if data["business_name"]:
        lines.append(f"Название: {data['business_name']}")
    if data["short_description"]:
        lines.append(f"Описание: {data['short_description']}")
    if data["specializations"]:
        lines.append("Специализации: " + ", ".join(data["specializations"]))
    if data["destinations"]:
        lines.append("Направления: " + ", ".join(data["destinations"]))
    if data["preferred_terms"]:
        lines.append("Предпочтительные формулировки: " + ", ".join(data["preferred_terms"]))
    if data["verified_claims"]:
        lines.append(
            "Подтверждённые факты: "
            + "; ".join(claim["text"] for claim in data["verified_claims"])
        )

    if len(lines) == 1:
        return ""

    return "\n".join(lines)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "travel-ai-orchestrator-web",
    }


@app.get("/api/competitors")
async def list_competitors():
    try:
        competitors = await competitor_repository.list_for_workspace(WEB_WORKSPACE_ID)
        last_analyzed = await competitor_repository.list_intelligence_dates_for_workspace(
            WEB_WORKSPACE_ID
        )

        return {
            "competitors": [
                {
                    "id": competitor.id,
                    "label": competitor.label,
                    "domain": canonical_domain(competitor.url),
                    "url": competitor.url,
                    "last_analyzed_at": last_analyzed.get(competitor.id),
                }
                for competitor in competitors
            ]
        }

    except Exception:
        return {"error": "Не удалось загрузить список конкурентов."}


@app.get("/api/competitors/{competitor_id}/intelligence")
async def get_competitor_intelligence(competitor_id: int):
    try:
        competitor = await competitor_repository.get_for_workspace(
            WEB_WORKSPACE_ID, competitor_id,
        )

        if competitor is None:
            return {"error": "Конкурент не найден.", "competitor": None, "intelligence": None}

        intelligence = await competitor_repository.get_intelligence(
            WEB_WORKSPACE_ID, competitor_id,
        )

        return {
            "competitor": {
                "id": competitor.id,
                "label": competitor.label,
                "domain": canonical_domain(competitor.url),
                "url": competitor.url,
            },
            "intelligence": asdict(intelligence) if intelligence is not None else None,
        }

    except Exception:
        return {"error": "Не удалось загрузить отчёт по конкуренту."}


@app.get("/api/signals")
async def list_signals():
    """Read-only: свежие сигналы Radar для текущего workspace.

    Использует ровно тот же путь чтения, что и Telegram-хэндлер
    on_find_signals() (app/handlers/menu.py) - list_for_workspace() затем
    build_workspace_signals() с теми же лимитами (200 -> 5). В отличие от
    Telegram-хэндлера здесь намеренно НЕ вызывается sync_eligible(): это
    write-операция (материализация новых interpretation-строк), а этот
    эндпоинт должен оставаться строго read-only. Синхронизация уже
    выполняется процессом бота (app/main.py, при старте и при каждом
    /find_signals) в ту же общую БД. Никакого нового LLM-вызова - и
    list_for_workspace(), и build_workspace_signals() только читают и
    фильтруют уже сохранённые данные.
    """
    try:
        records = await workspace_signal_repository.list_for_workspace(
            WEB_WORKSPACE_ID, limit=200,
        )
        signals = build_workspace_signals(lead_radar_config, records, limit=5)

        if signals is None:
            return {"error": "Радар сигналов сейчас недоступен.", "signals": []}

        source_names = {
            record.interpretation_id: record.source_name for record in records
        }

        return {
            "signals": [
                {
                    "id": signal.id,
                    "title": signal.title or "(без заголовка)",
                    "category": signal.category,
                    "category_label": category_label(signal.category),
                    "source_type": signal.source_type,
                    "source_name": source_names.get(signal.id) or "",
                    "created_at": signal.created_at,
                    "score": signal.score,
                    "url": signal.url,
                    "action_reason": signal.action_reason,
                }
                for signal in signals
            ]
        }

    except Exception:
        return {"error": "Не удалось загрузить сигналы.", "signals": []}


@app.get("/api/knowledge")
async def list_knowledge():
    """Read-only browse of the shared Travel Advantage/MWR Life knowledge
    base - the same repository the Assistant already reads for chat answers
    (knowledge_service.retrieve()). Not workspace-scoped by design: this is
    shared reference data with no workspace_id column, exactly like the
    existing chat retrieval path.
    """
    try:
        sources = await knowledge_repository.get_sources()
        items = await knowledge_repository.list_items()
        sources_by_id = {source.id: source for source in sources}

        return {
            "sources": [
                {
                    "id": source.id,
                    "title": source.title,
                    "source_type": source.source_type,
                    "source_name": source.source_name,
                    "verification_status": source.verification_status,
                    "version": source.version,
                    "effective_date": source.effective_date,
                }
                for source in sources
            ],
            "items": [
                {
                    "stable_key": item.stable_key,
                    "category": item.category,
                    "title": item.title,
                    "content": item.content,
                    "tags": list(item.tags),
                    "source_title": (
                        sources_by_id[item.source_id].title
                        if item.source_id in sources_by_id else None
                    ),
                    "verification_status": (
                        sources_by_id[item.source_id].verification_status
                        if item.source_id in sources_by_id else None
                    ),
                }
                for item in items
            ],
        }

    except Exception:
        return {"error": "Не удалось загрузить базу знаний.", "sources": [], "items": []}


def _material_payload(artifact) -> dict:
    return {
        "id": artifact.id,
        "title": artifact.title,
        "artifact_type": artifact.artifact_type,
        "status": artifact.status,
        "created_at": artifact.created_at,
        "updated_at": artifact.updated_at,
    }


def _version_payload(version) -> dict | None:
    if version is None:
        return None
    return {
        "id": version.id,
        "version_number": version.version_number,
        "content": version.content,
        "generation_note": version.generation_note,
        "created_at": version.created_at,
    }


class MaterialUpdateRequest(BaseModel):
    content: str
    expected_version_id: int


@app.get("/api/materials")
async def list_materials():
    """Read-only: реально сохранённые Artifact текущего workspace - тот же
    ArtifactRepository и та же логика, что и в Telegram «📚 Мои материалы»
    (app/handlers/materials.py)."""
    try:
        artifacts = await artifact_repository.list_artifacts(WEB_WORKSPACE_ID, limit=50)

        return {"materials": [_material_payload(artifact) for artifact in artifacts]}

    except Exception:
        return {"error": "Не удалось загрузить материалы.", "materials": []}


@app.get("/api/materials/{artifact_id}")
async def get_material(artifact_id: int):
    try:
        artifact = await artifact_repository.get_artifact(WEB_WORKSPACE_ID, artifact_id)

        if artifact is None:
            return {"error": "Материал не найден.", "material": None, "version": None}

        version = await artifact_repository.get_current_artifact_version(
            WEB_WORKSPACE_ID, artifact_id,
        )

        return {
            "material": _material_payload(artifact),
            "version": _version_payload(version),
        }

    except Exception:
        return {"error": "Не удалось загрузить материал.", "material": None, "version": None}


@app.put("/api/materials/{artifact_id}")
async def update_material(artifact_id: int, request: MaterialUpdateRequest):
    """Редактирование материала = новая версия поверх той же модели
    Artifact/ArtifactVersion, ровно тот же паттерн, что и в Telegram Safety
    Layer edit-флоу (app/handlers/text_review.py:
    add_artifact_version_if_current) - никакого параллельного хранилища.

    expected_version_id обязателен и защищает от потери чужих правок:
    если текущая версия материала успела измениться между открытием и
    сохранением, запись не проходит и клиенту возвращается понятная ошибка
    вместо тихой перезаписи.
    """
    content = request.content.strip()

    if not content:
        return {"error": "Текст материала не должен быть пустым.", "material": None, "version": None}

    try:
        artifact = await artifact_repository.get_artifact(WEB_WORKSPACE_ID, artifact_id)

        if artifact is None:
            return {"error": "Материал не найден.", "material": None, "version": None}

        new_version = await artifact_repository.add_artifact_version_if_current(
            WEB_WORKSPACE_ID, artifact_id, request.expected_version_id, content,
            generation_note="Отредактировано в веб-кабинете",
        )

        if new_version is None:
            return {
                "error": "Материал изменился в другом месте. Обновите страницу и попробуйте снова.",
                "material": None,
                "version": None,
            }

        updated_artifact = await artifact_repository.get_artifact(WEB_WORKSPACE_ID, artifact_id)

        return {
            "material": _material_payload(updated_artifact),
            "version": _version_payload(new_version),
        }

    except Exception:
        return {"error": "Не удалось сохранить материал.", "material": None, "version": None}


@app.delete("/api/materials/{artifact_id}")
async def delete_material(artifact_id: int):
    """Безопасное удаление: workspace isolation обеспечивается тем же
    механизмом, что и везде в ArtifactRepository (WHERE workspace_id=? AND
    id=? внутри delete_artifact - чужой artifact_id просто не совпадёт ни с
    одной строкой). Подтверждение - ответственность UI (двухшаговое
    подтверждение перед отправкой запроса), не самого эндпоинта."""
    try:
        deleted = await artifact_repository.delete_artifact(WEB_WORKSPACE_ID, artifact_id)

        if not deleted:
            return {"error": "Материал не найден.", "deleted": False}

        return {"deleted": True}

    except Exception:
        return {"error": "Не удалось удалить материал.", "deleted": False}


_ARTIFACT_STATUSES = ("draft", "review_required", "ready", "used", "archived")


@app.get("/api/history")
async def get_history():
    """Read-only activity view for «История / Артефакты»: real recorded AI
    usage events (usage_ledger_repository - already populated by every
    chat/competitor-analysis call, see record_llm_call()) plus a status
    breakdown of real saved Artifacts. Deliberately not a chat-message
    history - the Assistant's conversation is only kept in the browser's
    sessionStorage today, nothing server-side to read here honestly.
    """
    try:
        events = await usage_ledger_repository.list_for_workspace(
            WEB_WORKSPACE_ID, limit=30,
        )
        artifacts = await artifact_repository.list_artifacts(WEB_WORKSPACE_ID, limit=200)

        status_counts = {status: 0 for status in _ARTIFACT_STATUSES}
        for artifact in artifacts:
            status_counts[artifact.status] = status_counts.get(artifact.status, 0) + 1

        return {
            "usage_events": [
                {
                    "occurred_at": event.occurred_at,
                    "module": event.module,
                    "provider": event.provider,
                    "model": event.model,
                    "status": event.status.value,
                }
                for event in events
            ],
            "artifact_status_counts": status_counts,
        }

    except Exception:
        return {
            "error": "Не удалось загрузить историю.",
            "usage_events": [],
            "artifact_status_counts": {},
        }


def _business_profile_payload(profile) -> dict | None:
    if profile is None:
        return None
    context = profile.context
    return {
        "business_name": profile.business_name,
        "business_type": profile.business_type,
        "short_description": profile.short_description,
        "profile_status": profile.profile_status,
        "ta_affiliated": profile.ta_affiliated,
        "specializations": list(context.specializations),
        "destinations": list(context.destinations),
        "region": context.region,
        "audiences": list(context.audiences),
        "tone": str(context.communication.get("tone") or ""),
        "public_contacts": dict(context.public_contacts),
        "verified_claims": [
            claim.text for claim in context.claims
            if claim.verification_status == "verified" and claim.text.strip()
        ],
    }


def _personal_style_payload(preferences) -> dict | None:
    if preferences is None:
        return None
    return {
        "style_description": preferences.style_description,
        "example_posts": list(preferences.example_posts),
        "avoid_phrases": list(preferences.avoid_phrases),
    }


class BusinessProfileUpdateRequest(BaseModel):
    business_name: str
    business_type: str
    short_description: str
    specializations: list[str] = Field(default_factory=list)
    destinations: list[str] = Field(default_factory=list)
    region: str = ""
    audiences: list[str] = Field(default_factory=list)
    tone: str = ""


class PersonalStyleUpdateRequest(BaseModel):
    style_description: str
    avoid_phrases: list[str] = Field(default_factory=list)


class ExamplePostRequest(BaseModel):
    text: str


@app.get("/api/profile")
async def get_profile():
    """Read-only: реальный BusinessProfile workspace + личный стиль текущего
    пользователя (WorkspaceUserPreferences) - те же данные, что уже
    показывает Telegram «⚙️ Профиль». workspace_memory сюда намеренно не
    попадает: это внутренний контекст Ассистента (см. /api/chat), а не
    пользовательское профильное поле - пользователю оно не показывается."""
    try:
        profile = await partner_repository.get_business_profile(WEB_WORKSPACE_ID)
        preferences = await partner_repository.get_user_preferences(
            WEB_WORKSPACE_ID, WEB_TELEGRAM_USER_ID,
        )

        return {
            "business_profile": _business_profile_payload(profile),
            "personal_style": _personal_style_payload(preferences),
        }

    except Exception:
        return {
            "error": "Не удалось загрузить профиль.",
            "business_profile": None,
            "personal_style": None,
        }


@app.put("/api/profile/business")
async def update_business_profile_endpoint(request: BusinessProfileUpdateRequest):
    """Правки бизнес-профиля идут через тот же BusinessProfileService и тот
    же revision-based optimistic concurrency, что и Telegram self-service
    «🏢 Профиль компании» (app/handlers/profile.py) - никакой параллельной
    модели профиля. resolve_workspace_context() - тот же access-control
    (owner/admin, active workspace), что использует Telegram; отсутствие
    активного membership для WEB_TELEGRAM_USER_ID однозначно трактуется как
    запрет записи, а не как повод создать что-то новое.

    После сохранения /api/chat читает BusinessProfile заново на каждый
    запрос (без кеша) - новые значения сразу видны Ассистенту.
    """
    try:
        workspace_context = await partner_repository.resolve_workspace_context(
            WEB_TELEGRAM_USER_ID,
        )
        if workspace_context is None or workspace_context.workspace_id != WEB_WORKSPACE_ID:
            return {"error": "Недостаточно прав для изменения профиля.", "business_profile": None}

        profile = await partner_repository.get_business_profile(WEB_WORKSPACE_ID)
        if profile is None:
            return {"error": "Профиль ещё не создан.", "business_profile": None}

        context_dict = business_context_to_dict(profile.context)
        context_dict["specializations"] = request.specializations
        context_dict["destinations"] = request.destinations
        context_dict["region"] = request.region
        context_dict["audiences"] = request.audiences
        context_dict["communication"]["tone"] = request.tone

        # ta_affiliated workspaces can't change business_type from the web
        # either - same rule as Telegram's on_profile_field_selected().
        business_type = profile.business_type if profile.ta_affiliated else request.business_type

        updated = await BusinessProfileService(partner_repository).update(
            workspace_context, profile.revision,
            business_name=request.business_name, business_type=business_type,
            short_description=request.short_description, context=context_dict,
        )

        return {"business_profile": _business_profile_payload(updated)}

    except BusinessProfileAccessError:
        return {"error": "Недостаточно прав для изменения профиля.", "business_profile": None}
    except (BusinessProfileValidationError, StaleBusinessProfileError) as exc:
        return {"error": str(exc), "business_profile": None}
    except Exception:
        return {"error": "Не удалось сохранить профиль.", "business_profile": None}


@app.put("/api/profile/style")
async def update_personal_style(request: PersonalStyleUpdateRequest):
    """Личный стиль - через существующие PartnerRepository-методы
    (WorkspaceUserPreferences), те же, что Telegram «✍️ Мой стиль общения» /
    «🚫 Чего не использовать». Каждый метод сам перечитывает текущую запись
    и сохраняет остальные поля как есть, так что вызовы ниже не затирают
    example_posts. /api/chat читает эти же предпочтения на каждый запрос -
    сохранённый стиль сразу используется Ассистентом."""
    try:
        await partner_repository.set_user_style_description(
            WEB_WORKSPACE_ID, WEB_TELEGRAM_USER_ID, request.style_description,
        )
        preferences = await partner_repository.set_user_avoid_phrases(
            WEB_WORKSPACE_ID, WEB_TELEGRAM_USER_ID, request.avoid_phrases,
        )
        return {"personal_style": _personal_style_payload(preferences)}

    except Exception:
        return {"error": "Не удалось сохранить личный стиль.", "personal_style": None}


@app.post("/api/profile/style/examples")
async def add_personal_style_example(request: ExamplePostRequest):
    text = request.text.strip()

    if not text:
        return {"error": "Текст примера не должен быть пустым.", "personal_style": None}

    try:
        preferences = await partner_repository.add_user_example_post(
            WEB_WORKSPACE_ID, WEB_TELEGRAM_USER_ID, text,
        )
        return {"personal_style": _personal_style_payload(preferences)}

    except TooManyUserExamplesError as exc:
        return {"error": str(exc), "personal_style": None}
    except Exception:
        return {"error": "Не удалось сохранить пример текста.", "personal_style": None}


@app.delete("/api/profile/style/examples")
async def clear_personal_style_examples():
    try:
        preferences = await partner_repository.clear_user_example_posts(
            WEB_WORKSPACE_ID, WEB_TELEGRAM_USER_ID,
        )
        return {"personal_style": _personal_style_payload(preferences)}

    except Exception:
        return {"error": "Не удалось очистить примеры.", "personal_style": None}


@app.post("/api/chat")
async def chat(request: ChatRequest):
    message = request.message.strip()

    if not message:
        return {"error": "Введите вопрос."}

    try:
        recent_user_context = [
            item.get("content", "")
            for item in request.history[-6:]
            if item.get("role") == "user"
        ]

        retrieval_query = "\n".join(
            [
                *recent_user_context,
                message,
            ]
        )

        bundle = await knowledge_service.retrieve(retrieval_query)
        knowledge_context = _knowledge_context(bundle)

        business_profile = await partner_repository.get_business_profile(WEB_WORKSPACE_ID)
        if business_profile is not None:
            knowledge_context = "\n\n".join(
                part for part in (
                    knowledge_context,
                    _business_profile_context(business_profile),
                )
                if part
            )

        competitor = await _requested_competitor(message)

        if competitor is not None:
            try:
                intelligence = await competitor_intelligence_service.analyze(
                    competitor
                )

                await competitor_repository.save_intelligence(
                    WEB_WORKSPACE_ID,
                    intelligence,
                )

                knowledge_context = "\n\n".join(
                    part for part in (
                        knowledge_context,
                        _competitor_context(intelligence),
                    )
                    if part
                )

            except CompetitorIntelligenceUnavailable as exc:
                knowledge_context = "\n\n".join(
                    part for part in (
                        knowledge_context,
                        "=== COMPETITOR INTELLIGENCE ===\n"
                        f"Свежий анализ недоступен: {exc}. "
                        "Не выдавай общие знания модели за свежие данные.",
                    )
                    if part
                )

        preferences = await partner_repository.get_user_preferences(
            WEB_WORKSPACE_ID,
            WEB_TELEGRAM_USER_ID,
        )

        personal_style = (
            preferences.style_description.strip()
            if preferences is not None
            else ""
        )

        memory_record = await workspace_memory_repository.get(WEB_WORKSPACE_ID)
        workspace_memory_text = (
            memory_record.summary.strip() if memory_record is not None else ""
        )

        if len(workspace_memory_text) > MAX_WORKSPACE_MEMORY_CHARS:
            workspace_memory_text = (
                workspace_memory_text[:MAX_WORKSPACE_MEMORY_CHARS] + "…"
            )

        try:
            chat_result = await asyncio.to_thread(
                chat_provider.generate,
                message=message,
                history=request.history[-12:],
                knowledge_context=knowledge_context,
                personal_style=personal_style,
                workspace_memory=workspace_memory_text,
            )
        except Exception:
            await record_llm_call(
                usage_ledger_repository,
                workspace_id=WEB_WORKSPACE_ID,
                telegram_user_id=None,
                module="web_chat",
                provider="openai",
                model="gpt-5.6-terra",
                usage=None,
                status=UsageStatus.FAILURE,
            )
            raise

        await record_llm_call(
            usage_ledger_repository,
            workspace_id=WEB_WORKSPACE_ID,
            telegram_user_id=None,
            module="web_chat",
            provider="openai",
            model="gpt-5.6-terra",
            usage=chat_result.usage,
            status=UsageStatus.SUCCESS,
        )

        answer = chat_result.text

        clean_answer = (
            answer
            .replace("&amp;#x20;", " ")
            .replace("&amp;#32;", " ")
            .replace("&amp;nbsp;", " ")
            .replace("&#x20;", " ")
            .replace("&#32;", " ")
            .replace("&nbsp;", " ")
            .replace("\u00a0", " ")
        )

        answer_html = markdown.markdown(
            clean_answer,
            extensions=["tables", "fenced_code", "sane_lists"],
        )

        return {
            "answer": clean_answer,
            "answer_html": answer_html,
            "model": "gpt-5.6-terra",
            "knowledge_used": bool(knowledge_context),
            "knowledge_sources": [
                {
                    "title": source.title,
                    "reference": source.source_reference,
                    "verification_status": source.verification_status,
                }
                for source in bundle.sources[:8]
            ],
        }

    except Exception:
        return {
            "error": "Не удалось получить ответ AI. Попробуйте ещё раз."
        }


@app.get("/", response_class=HTMLResponse)
async def home():
    return Path(
        "app/templates/chat.html"
    ).read_text(encoding="utf-8")
