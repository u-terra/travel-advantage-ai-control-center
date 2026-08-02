from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.access import AllowlistMiddleware
from app.config import load_settings
from app.handlers import build_router
from app.services.content_factory import ContentFactoryConfig
from app.services.lead_radar import LeadRadarConfig
from app.storage import Journal


def _build_dispatcher(
    allowed_user_ids: frozenset[int],
    journal: Journal,
    content_factory_config: ContentFactoryConfig,
    lead_radar_config: LeadRadarConfig,
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
    return dp


async def _async_main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    journal = Journal(settings.journal_db_path)
    await journal.init()

    content_factory_config = ContentFactoryConfig(
        url=settings.content_factory_url,
        token=settings.content_factory_token,
        timeout_seconds=settings.content_factory_timeout_seconds,
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
    )

    await dp.start_polling(bot)


def run() -> None:
    asyncio.run(_async_main())
