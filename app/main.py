from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.access import AllowlistMiddleware
from app.access_state_gate import AccessStateMiddleware
from app.config import load_settings
from app.handlers import build_router
from app.onboarding_gate import OnboardingGateMiddleware
from app.orchestration.factory import create_orchestration_llm_provider
from app.orchestration.openai_provider import OrchestrationOpenAIConfig
from app.orchestration.provider import OrchestrationLLMProvider
from app.planner.cost import DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN
from app.planner.factory import create_planner_llm_provider
from app.planner.openai_provider import PlannerOpenAIConfig
from app.planner.provider import PlannerLLMProvider
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.conversation_state_repository import ConversationStateRepository
from app.repositories.knowledge_repository import KnowledgeRepository
from app.repositories.partner_repository import PartnerRepository
from app.repositories.source_analysis_repository import SourceAnalysisRepository
from app.repositories.source_catalog_repository import SourceCatalogRepository
from app.repositories.work_repository import WorkRepository
from app.repositories.workspace_signal_repository import WorkspaceSignalRepository
from app.services.chat_serialization import ChatSerializationMiddleware
from app.services.content_factory import ContentFactoryConfig
from app.services.lead_radar import LeadRadarConfig
from app.services.knowledge_service import KnowledgeService
from app.services.llm.base import LLMProvider
from app.services.llm.factory import create_llm_provider
from app.services.reference_resolver import ReferenceResolver
from app.services.source_registry import SEED_REGISTRY_PATH
from app.storage import Journal
from app.workspace_context import WorkspaceContextMiddleware


def _build_dispatcher(
    allowed_user_ids: frozenset[int],
    journal: Journal,
    llm_provider: LLMProvider,
    lead_radar_config: LeadRadarConfig,
    v2_menu_enabled: bool = False,
    partner_repository: PartnerRepository | None = None,
    artifact_repository: ArtifactRepository | None = None,
    source_analysis_repository: SourceAnalysisRepository | None = None,
    source_catalog_repository: SourceCatalogRepository | None = None,
    workspace_signal_repository: WorkspaceSignalRepository | None = None,
    competitor_repository: CompetitorRepository | None = None,
    work_repository: WorkRepository | None = None,
    conversation_state_repository: ConversationStateRepository | None = None,
    onboarding_rollout_at: datetime | None = None,
    orchestration_llm_provider: OrchestrationLLMProvider | None = None,
    planner_llm_provider: PlannerLLMProvider | None = None,
    planner_enabled: bool = False,
    planner_allowed_telegram_user_ids: frozenset[int] = frozenset(),
    planner_max_llm_calls: int = DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN,
    reference_resolver: ReferenceResolver | None = None,
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    # Единый централизованный guard доступа. Outer-middleware срабатывает раньше
    # любых фильтров и хендлеров и охватывает команды, обычные сообщения и
    # callback-кнопки. Посторонний не доходит до логики панели управления.
    guard = AllowlistMiddleware(allowed_user_ids)
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)
    if partner_repository is not None:
        workspace_context = WorkspaceContextMiddleware(partner_repository)
        dp.message.outer_middleware(workspace_context)
        dp.callback_query.outer_middleware(workspace_context)
        # F1 Conversation Core Foundation: serializes updates per
        # (workspace_id, telegram_user_id) so two fast consecutive messages
        # from the same user can't be handled out of order/in parallel (see
        # app.services.chat_serialization). Needs workspace_context already
        # resolved, so it sits right after it and before the access/
        # onboarding gates - purely additive, no behavior change for any
        # existing flow.
        chat_serialization = ChatSerializationMiddleware()
        dp.message.outer_middleware(chat_serialization)
        dp.callback_query.outer_middleware(chat_serialization)
        # Требует уже готовый workspace_context — регистрируется следом,
        # тем же принципом, что и WorkspaceContextMiddleware. Stage 3A:
        # решает, доступен ли рабочий Оркестратор (workspace + active/
        # trial_active) или нужно лобби — независимо от allowlist.
        access_state_gate = AccessStateMiddleware(partner_repository)
        dp.message.outer_middleware(access_state_gate)
        dp.callback_query.outer_middleware(access_state_gate)
        onboarding_gate = OnboardingGateMiddleware(partner_repository, onboarding_rollout_at)
        dp.message.outer_middleware(onboarding_gate)
        dp.callback_query.outer_middleware(onboarding_gate)

    dp.include_router(build_router())

    dp["journal"] = journal
    dp["llm_provider"] = llm_provider
    dp["lead_radar_config"] = lead_radar_config
    dp["v2_menu_enabled"] = v2_menu_enabled
    dp["partner_repository"] = partner_repository
    dp["artifact_repository"] = artifact_repository
    dp["source_analysis_repository"] = source_analysis_repository
    dp["source_catalog_repository"] = source_catalog_repository
    dp["workspace_signal_repository"] = workspace_signal_repository
    dp["competitor_repository"] = competitor_repository
    dp["work_repository"] = work_repository
    # F1 Conversation Core Foundation - infrastructure only, see
    # app.repositories.conversation_state_repository. No handler reads or
    # writes through this yet.
    dp["conversation_state_repository"] = conversation_state_repository
    # Phase 1 LLM orchestration shadow mode - see app.orchestration. Defaults
    # to the inert NullOrchestrationLLMProvider when not passed explicitly,
    # same as every other optional dependency here.
    dp["orchestration_llm_provider"] = (
        orchestration_llm_provider or create_orchestration_llm_provider(None)
    )
    # Stage 3 Planner MVP - see app.planner. Defaults to the inert
    # NullPlannerLLMProvider/disabled flag/empty allowlist when not passed
    # explicitly, same pattern as orchestration_llm_provider above: existing
    # callers/tests that don't pass these are entirely unaffected.
    dp["planner_llm_provider"] = planner_llm_provider or create_planner_llm_provider(None)
    dp["planner_enabled"] = planner_enabled
    dp["planner_allowed_telegram_user_ids"] = planner_allowed_telegram_user_ids
    dp["planner_max_llm_calls"] = planner_max_llm_calls
    dp["reference_resolver"] = reference_resolver
    return dp


async def _async_main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    partner_repository = PartnerRepository(settings.journal_db_path)
    await partner_repository.init()
    owner_membership = await partner_repository.bootstrap_owner_membership(
        settings.admin_telegram_id
    )

    journal = Journal(settings.journal_db_path)
    await journal.init(
        owner_membership.workspace_id if owner_membership is not None else None
    )

    artifact_repository = ArtifactRepository(settings.journal_db_path)
    await artifact_repository.init()

    source_analysis_repository = SourceAnalysisRepository(settings.journal_db_path)
    await source_analysis_repository.initialize()

    source_catalog_repository = SourceCatalogRepository(
        settings.journal_db_path, settings.sources_registry_path
    )
    legacy_source_path = (
        settings.sources_registry_path
        if settings.sources_registry_path.exists()
        else SEED_REGISTRY_PATH
    )
    await source_catalog_repository.init(
        owner_membership.workspace_id if owner_membership is not None else None,
        legacy_path=legacy_source_path,
    )

    workspace_signal_repository = WorkspaceSignalRepository(
        settings.journal_db_path, settings.lead_radar_db_path
    )
    await workspace_signal_repository.init(
        owner_membership.workspace_id if owner_membership is not None else None
    )
    await workspace_signal_repository.sync_eligible()

    competitor_repository = CompetitorRepository(settings.journal_db_path)
    await competitor_repository.init()

    work_repository = WorkRepository(settings.journal_db_path)
    await work_repository.init()

    conversation_state_repository = ConversationStateRepository(settings.journal_db_path)
    await conversation_state_repository.init()

    knowledge_repository = KnowledgeRepository()
    await knowledge_repository.init()
    reference_resolver = ReferenceResolver(KnowledgeService(knowledge_repository))

    content_factory_config = ContentFactoryConfig(
        url=settings.content_factory_url,
        token=settings.content_factory_token,
        timeout_seconds=settings.content_factory_timeout_seconds,
        source_analysis_url=settings.content_factory_source_analysis_url,
        topics_url=settings.content_factory_topics_url,
    )
    # Неизвестный LLM_PROVIDER — ошибка на старте, а не молчаливый уход
    # не к тому вендору.
    llm_provider = create_llm_provider(
        settings.llm_provider, content_factory_config=content_factory_config
    )

    lead_radar_config = LeadRadarConfig(
        db_path=settings.lead_radar_db_path,
    )

    # Phase 1 LLM orchestration shadow mode - defaults to "null" (fully
    # inert) unless ORCHESTRATION_LLM_PROVIDER is set. See app.orchestration.
    orchestration_openai_config = OrchestrationOpenAIConfig(
        api_key=settings.orchestration_openai_api_key,
        model=settings.orchestration_openai_model,
        timeout_seconds=settings.orchestration_openai_timeout_seconds,
    )
    orchestration_llm_provider = create_orchestration_llm_provider(
        settings.orchestration_llm_provider,
        openai_config=orchestration_openai_config,
    )

    # Stage 3 Planner MVP - defaults to "null" (fully inert) unless
    # PLANNER_LLM_PROVIDER is set. See app.planner. Неизвестный
    # PLANNER_LLM_PROVIDER — ошибка на старте, а не молчаливый уход не к
    # тому вендору (тот же принцип, что и LLM_PROVIDER/ORCHESTRATION_LLM_PROVIDER
    # выше).
    planner_openai_config = PlannerOpenAIConfig(
        api_key=settings.planner_openai_api_key,
        model=settings.planner_openai_model,
        timeout_seconds=settings.planner_openai_timeout_seconds,
    )
    planner_llm_provider = create_planner_llm_provider(
        settings.planner_llm_provider,
        openai_config=planner_openai_config,
    )

    bot = Bot(settings.bot_token)
    dp = _build_dispatcher(
        settings.allowed_user_ids,
        journal,
        llm_provider,
        lead_radar_config,
        settings.v2_menu_enabled,
        partner_repository,
        artifact_repository,
        source_analysis_repository,
        source_catalog_repository,
        workspace_signal_repository,
        competitor_repository,
        work_repository,
        conversation_state_repository=conversation_state_repository,
        onboarding_rollout_at=settings.onboarding_rollout_at,
        orchestration_llm_provider=orchestration_llm_provider,
        planner_llm_provider=planner_llm_provider,
        planner_enabled=settings.planner_enabled,
        planner_allowed_telegram_user_ids=settings.planner_allowed_telegram_user_ids,
        planner_max_llm_calls=settings.planner_max_llm_calls,
        reference_resolver=reference_resolver,
    )

    await dp.start_polling(bot)


def run() -> None:
    asyncio.run(_async_main())
