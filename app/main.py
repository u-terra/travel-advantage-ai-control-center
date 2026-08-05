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
from app.storage import Journal


def _build_dispatcher(
    allowed_user_ids: frozenset[int],
    journal: Journal,
    content_factory_config: ContentFactoryConfig,
    lead_radar_config: LeadRadarConfig,
    v2_menu_enabled: bool = False,
    partner_repository: PartnerRepository | None = None,
    artifact_repository: ArtifactRepository | None = None,
    source_analysis_repository: SourceAnalysisRepository | None = None,
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    # Единый централизованный guard доступа. Outer-middleware срабатывает раньше
    # любых фильтров и хендлеров и охватывает команды, обычные сообщения и
    # callback-кнопки. Посторонний не доходит до логики панели управления.
    guard = AllowlistMiddleware(allowed_user_ids)
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)

    dp.include_router(build_router())

    dp["journal"] = journal
    dp["content_factory_config"] = content_factory_config
    dp["lead_radar_config"] = lead_radar_config
    dp["v2_menu_enabled"] = v2_menu_enabled
    dp["partner_repository"] = partner_repository
    dp["artifact_repository"] = artifact_repository
    dp["source_analysis_repository"] = source_analysis_repository
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
    await partner_repository.ensure_owner_workspace(settings.admin_telegram_id)

    artifact_repository = ArtifactRepository(settings.journal_db_path)
    await artifact_repository.init()

    source_analysis_repository = SourceAnalysisRepository(settings.journal_db_path)
    await source_analysis_repository.initialize()

    content_factory_config = ContentFactoryConfig(
        url=settings.content_factory_url,
        token=settings.content_factory_token,
        timeout_seconds=settings.content_factory_timeout_seconds,
        source_analysis_url=settings.content_factory_source_analysis_url,
    )

    lead_radar_config = LeadRadarConfig(
        db_path=settings.lead_radar_db_path,
    )

    bot = Bot(settings.bot_token)
    dp = _build_dispatcher(
        settings.allowed_user_ids,
        journal,
        content_factory_config,
        lead_radar_config,
        settings.v2_menu_enabled,
        partner_repository,
        artifact_repository,
        source_analysis_repository,
    )

    await dp.start_polling(bot)


def run() -> None:
    asyncio.run(_async_main())
