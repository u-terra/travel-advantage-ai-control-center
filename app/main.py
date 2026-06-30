from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardRemove

from app.config import load_settings
from app.filters import IsAdmin
from app.handlers import build_router
from app.services.content_factory import ContentFactoryConfig
from app.services.lead_radar import LeadRadarConfig
from app.storage import Journal


_STRANGER_REPLY = "Этот бот предназначен для внутренней работы владельца системы."


def _build_dispatcher(
    admin_id: int,
    journal: Journal,
    content_factory_config: ContentFactoryConfig,
    lead_radar_config: LeadRadarConfig,
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    owner_router = build_router()
    owner_router.message.filter(IsAdmin(admin_id))
    dp.include_router(owner_router)

    stranger = Router(name="stranger")

    @stranger.message()
    async def on_stranger(message: Message) -> None:
        await message.answer(_STRANGER_REPLY, reply_markup=ReplyKeyboardRemove())

    dp.include_router(stranger)

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
        settings.admin_telegram_id,
        journal,
        content_factory_config,
        lead_radar_config,
    )

    await dp.start_polling(bot)


def run() -> None:
    asyncio.run(_async_main())
