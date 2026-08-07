"""Точка входа «Дебиторка-бот» (Stage 1: каркас приложения, без Docker).

Запуск: `python -m app.main` внутри активированного venv,
с переменными окружения из `.env` (см. `.env.example`).
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import get_root_router
from app.bot.middlewares.allowlist import AllowlistMiddleware
from app.bot.middlewares.database import DatabaseSessionMiddleware
from app.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


def create_dispatcher(
    *,
    allowed_user_id: int,
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
        AllowlistMiddleware(allowed_user_id=allowed_user_id)
    )
    dispatcher.update.outer_middleware(
        DatabaseSessionMiddleware(session_maker=session_maker)
    )
    dispatcher.include_router(get_root_router())
    return dispatcher


async def main() -> None:
    settings = get_settings()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = create_dispatcher(allowed_user_id=settings.allowed_user_id)

    logger.info("Дебиторка-бот запускается (allowed_user_id=%s)", settings.allowed_user_id)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
