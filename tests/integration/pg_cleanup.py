"""Общий destructive cleanup Stage 3 таблиц для integration-тестов."""
from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import (
    AuditCycle,
    Counterparty,
    DebtPosition,
    ManagerGroup,
    SourceFile,
)
from tests.integration.db_safety import assert_destructive_cleanup_allowed


async def clean_stage3_tables(
    maker: async_sessionmaker[AsyncSession],
    *,
    db_name: str,
) -> None:
    """DELETE всех Stage 3 строк; только после ``assert_destructive_cleanup_allowed``."""
    assert_destructive_cleanup_allowed(db_name)
    async with maker() as session:
        await session.execute(delete(DebtPosition))
        await session.execute(delete(SourceFile))
        await session.execute(delete(Counterparty))
        await session.execute(delete(ManagerGroup))
        await session.execute(delete(AuditCycle))
        await session.commit()
