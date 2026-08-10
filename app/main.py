from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.access import AllowlistMiddleware
from app.config import load_settings
from app.handlers import build_router
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository
from app.repositories.source_analysis_repository import SourceAnalysisRepository
from app.services.content_factory import ContentFactoryConfig
from app.services.lead_radar import LeadRadarConfig
from app.services.llm.base import LLMProvider
from app.services.llm.factory import create_llm_provider
from app.services.source_registry_store import SourceRegistryStore
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
    source_registry_store: SourceRegistryStore | None = None,
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

    dp.include_router(build_router())

    dp["journal"] = journal
    dp["llm_provider"] = llm_provider
    dp["lead_radar_config"] = lead_radar_config
    dp["v2_menu_enabled"] = v2_menu_enabled
    dp["partner_repository"] = partner_repository
    dp["artifact_repository"] = artifact_repository
    dp["source_analysis_repository"] = source_analysis_repository
    dp["source_registry_store"] = source_registry_store or SourceRegistryStore()
    return dp


async def _async_main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    journal = Journal(settings.journal_db_path)
    await journal.init()

    partner_repository = PartnerRepository(settings.journal_db_path)
    await partner_repository.init()
    await partner_repository.bootstrap_owner_membership(settings.admin_telegram_id)

    artifact_repository = ArtifactRepository(settings.journal_db_path)
    await artifact_repository.init()

    source_analysis_repository = SourceAnalysisRepository(settings.journal_db_path)
    await source_analysis_repository.initialize()

    # Первый запуск разворачивает рабочий реестр из стартового набора.
    # Существующий файл не трогается: источники, добавленные владельцем,
    # переживают обновление кода.
    source_registry_store = SourceRegistryStore(settings.sources_registry_path)
    await asyncio.to_thread(source_registry_store.ensure_bootstrapped)

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
        source_registry_store,
    )

    await dp.start_polling(bot)


def run() -> None:
    asyncio.run(_async_main())
