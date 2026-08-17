from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.access import AllowlistMiddleware
from app.config import load_settings
from app.handlers import build_router
from app.onboarding_gate import OnboardingGateMiddleware
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.partner_repository import PartnerRepository
from app.repositories.source_analysis_repository import SourceAnalysisRepository
from app.repositories.source_catalog_repository import SourceCatalogRepository
from app.repositories.work_repository import WorkRepository
from app.repositories.workspace_signal_repository import WorkspaceSignalRepository
from app.services.content_factory import ContentFactoryConfig
from app.services.lead_radar import LeadRadarConfig
from app.services.llm.base import LLMProvider
from app.services.llm.factory import create_llm_provider
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
    onboarding_rollout_at: datetime | None = None,
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
        # Требует уже готовый workspace_context — регистрируется следом,
        # тем же принципом, что и WorkspaceContextMiddleware.
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

    content_factory_config = ContentFactoryConfig(
        url=settings.content_factory_url,
        token=settings.content_factory_token,
        timeout_seconds=settings.content_factory_timeout_seconds,
        source_analysis_url=settings.content_factory_source_analysis_url,
    )
    # Неизвестный LLM_PROVIDER — ошибка на старте, а не молчаливый уход
    # не к тому вендору.
    llm_provider = create_llm_provider(
        settings.llm_provider, content_factory_config=content_factory_config
    )

    lead_radar_config = LeadRadarConfig(
        db_path=settings.lead_radar_db_path,
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
        settings.onboarding_rollout_at,
    )

    await dp.start_polling(bot)


def run() -> None:
    asyncio.run(_async_main())
