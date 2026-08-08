"""Точка входа «Дебиторка-бот» (Stage 1 + Stage 3.2 scheduler).

Запуск: `python -m app.main` внутри активированного venv,
с переменными окружения из `.env` (см. `.env.example`).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import get_root_router
from app.bot.middlewares.allowlist import AllowlistMiddleware
from app.bot.middlewares.database import DatabaseSessionMiddleware
from app.bot.scheduler.idle_scheduler import IdleReminderScheduler
from app.bot.scheduler.report_scheduler import ReportScheduler
from app.config import get_settings
from app.infrastructure.database.session import get_session_maker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


def create_dispatcher(
    *,
    allowed_user_ids: Iterable[int],
    session_maker: async_sessionmaker[AsyncSession] | None = None,
) -> Dispatcher:
    """Собирает Dispatcher с FSM MemoryStorage и SimpleEventIsolation.

    Без isolation два параллельных update одного чата оба видят пустой FSM и
    перезаписывают загрузку; SimpleEventIsolation сериализует обработку по ключу
    USER_IN_CHAT, поэтому второй документ получает «ещё не завершена».
    """
    dispatcher = Dispatcher(
        storage=MemoryStorage(),
        events_isolation=SimpleEventIsolation(),
    )
    dispatcher.update.outer_middleware(
        AllowlistMiddleware(allowed_user_ids=allowed_user_ids)
    )
    dispatcher.update.outer_middleware(
        DatabaseSessionMiddleware(session_maker=session_maker)
    )
    dispatcher.include_router(get_root_router())
    return dispatcher


async def main() -> None:
    settings = get_settings()
    session_maker = get_session_maker()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = create_dispatcher(
        allowed_user_ids=settings.allowed_user_ids,
        session_maker=session_maker,
    )
    scheduler = IdleReminderScheduler(
        bot=bot,
        session_maker=session_maker,
        settings=settings,
    )
    report_scheduler = ReportScheduler(
        bot=bot,
        session_maker=session_maker,
        settings=settings,
    )

    logger.info(
        "Дебиторка-бот запускается (allowed_user_ids=%s, notify_chat_id=%s)",
        sorted(settings.allowed_user_ids),
        settings.audit_notification_chat_id,
    )
    await scheduler.start()
    await report_scheduler.start()
    try:
        await dispatcher.start_polling(bot)
    finally:
        await report_scheduler.stop()
        await scheduler.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
