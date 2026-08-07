"""Асинхронное подключение к PostgreSQL (SQLAlchemy 2.0 + asyncpg)."""
from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    """Создаёт (единожды за процесс) асинхронный engine по настройкам окружения."""
    settings = get_settings()
    return create_async_engine(settings.database_url, pool_pre_ping=True)


@lru_cache
def get_session_maker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=get_engine(), expire_on_commit=True)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Асинхронный контекст сессии для использования в сервисах/хендлерах."""
    session_maker = get_session_maker()
    async with session_maker() as session:
        yield session
