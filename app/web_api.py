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
from app.domain.competitor_discovery import canonical_domain
from app.domain.usage import UsageStatus
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.knowledge_repository import KnowledgeRepository
from app.repositories.partner_repository import PartnerRepository
from app.repositories.usage_ledger_repository import UsageLedgerRepository
from app.repositories.workspace_memory_repository import WorkspaceMemoryRepository
from app.repositories.workspace_signal_repository import WorkspaceSignalRepository
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
