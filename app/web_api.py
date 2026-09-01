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
from app.services.competitor_intelligence import (
    CompetitorIntelligenceService,
    CompetitorIntelligenceUnavailable,
)
from app.services.content_factory import ContentFactoryConfig
from app.services.knowledge_service import KnowledgeBundle, KnowledgeService
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

# Temporary until web authentication is implemented.
WEB_WORKSPACE_ID = 1
WEB_TELEGRAM_USER_ID = 586249067

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

        try:
            chat_result = await asyncio.to_thread(
                chat_provider.generate,
                message=message,
                history=request.history[-12:],
                knowledge_context=knowledge_context,
                personal_style=personal_style,
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
