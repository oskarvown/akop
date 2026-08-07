from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.domain.models import (
    AuditCycle,
    Counterparty,
    DebtPosition,
    ManagerGroup,
    SourceFile,
)


@pytest.fixture
async def stage3_session_maker():
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    maker = async_sessionmaker(bind=engine, expire_on_commit=True)
    try:
        async with maker() as session:
            await session.execute(select(1))
    except (OperationalError, OSError) as exc:  # pragma: no cover - environment dependent
        # asyncpg may raise ConnectionRefusedError (OSError) before SQLAlchemy wraps it.
        await engine.dispose()
        pytest.skip(f"Локальный PostgreSQL недоступен: {exc}")

    await _clean(maker)
    try:
        yield maker
    finally:
        await _clean(maker)
        await engine.dispose()


@pytest.fixture
async def stage3_session(
    stage3_session_maker: async_sessionmaker[AsyncSession],
) -> AsyncSession:
    async with stage3_session_maker() as session:
        yield session
        await session.rollback()


async def _clean(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as session:
        await session.execute(delete(DebtPosition))
        await session.execute(delete(SourceFile))
        await session.execute(delete(Counterparty))
        await session.execute(delete(ManagerGroup))
        await session.execute(delete(AuditCycle))
        await session.commit()
