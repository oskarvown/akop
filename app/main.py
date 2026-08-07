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
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import get_root_router
from app.bot.middlewares.allowlist import AllowlistMiddleware
from app.bot.middlewares.database import DatabaseSessionMiddleware
from app.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())

    # Регистрация как outer middleware на `update` (а не на message/callback_query
    # по отдельности) гарантирует, что авторизация проверяется для любого текущего
    # и будущего типа события до маршрутизации в handler — см. docstring мидлвари.
    allowlist = AllowlistMiddleware(allowed_user_id=settings.allowed_user_id)
    dispatcher.update.outer_middleware(allowlist)
    dispatcher.update.outer_middleware(DatabaseSessionMiddleware())

    dispatcher.include_router(get_root_router())

    logger.info("Дебиторка-бот запускается (allowed_user_id=%s)", settings.allowed_user_id)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
