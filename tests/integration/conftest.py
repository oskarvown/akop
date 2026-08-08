from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from tests.integration.pg_cleanup import clean_stage3_tables


@pytest.fixture
async def stage3_session_maker():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    maker = async_sessionmaker(bind=engine, expire_on_commit=True)
    try:
        async with maker() as session:
            await session.execute(select(1))
    except (OperationalError, OSError) as exc:  # pragma: no cover - environment dependent
        # asyncpg may raise ConnectionRefusedError (OSError) before SQLAlchemy wraps it.
        await engine.dispose()
        pytest.skip(f"Локальный PostgreSQL недоступен: {exc}")

    await clean_stage3_tables(maker, db_name=settings.db_name)
    try:
        yield maker
    finally:
        await clean_stage3_tables(maker, db_name=settings.db_name)
        await engine.dispose()


@pytest.fixture
async def stage3_session(
    stage3_session_maker: async_sessionmaker[AsyncSession],
) -> AsyncSession:
    async with stage3_session_maker() as session:
        yield session
        await session.rollback()
