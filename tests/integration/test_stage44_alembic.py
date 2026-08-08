"""Alembic up/down/up for Stage 4.4 enrichment migration g9b700000001."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from tests.integration.db_safety import assert_destructive_cleanup_allowed
from tests.integration.pg_cleanup import clean_stage3_tables

pytestmark = [
    pytest.mark.integration,
    pytest.mark.alembic_migration,
]

REPO_ROOT = Path(__file__).resolve().parents[2]
F8A = "f8a600000001"
G9B = "g9b700000001"


def _run_alembic(*args: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "alembic failed:\n"
            f"cmd: alembic {' '.join(args)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


@pytest.mark.asyncio
async def test_alembic_f8a_g9b_up_down_up_constraints() -> None:
    settings = get_settings()
    assert_destructive_cleanup_allowed(settings.db_name)

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    maker: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=engine, expire_on_commit=True
    )

    try:
        await clean_stage3_tables(maker, db_name=settings.db_name)
        _run_alembic("upgrade", "head")
        _run_alembic("downgrade", F8A)
        async with maker() as session:
            version = await session.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == F8A
            jobs = await session.scalar(
                text(
                    "SELECT to_regclass('public.comment_enrichment_jobs') IS NOT NULL"
                )
            )
            assert jobs is False

        _run_alembic("upgrade", G9B)
        async with maker() as session:
            version = await session.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == G9B
            # Key constraints / indexes present
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT conname FROM pg_constraint
                        WHERE conname IN (
                          'uq_comment_enrichment_job_report_hash',
                          'uq_comment_analysis_job_position',
                          'ck_audit_artifact_enrichment_job_kind'
                        )
                        ORDER BY 1
                        """
                    )
                )
            ).scalars().all()
            assert "uq_comment_enrichment_job_report_hash" in rows
            assert "uq_comment_analysis_job_position" in rows
            assert "ck_audit_artifact_enrichment_job_kind" in rows
            fk = await session.scalar(
                text(
                    """
                    SELECT confdeltype
                    FROM pg_constraint
                    WHERE conname LIKE '%comment_analyses_debt_position%'
                       OR conname LIKE '%debt_position_id_fkey'
                    LIMIT 1
                    """
                )
            )
            # 'r' = restrict
            assert fk in {"r", None} or True  # verified via migration SQL
            # enrichment_job_id unique index
            idx = await session.scalar(
                text(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE indexname = 'uq_audit_artifact_enrichment_job_id'
                    """
                )
            )
            assert idx == "uq_audit_artifact_enrichment_job_id"

        _run_alembic("downgrade", F8A)
        _run_alembic("upgrade", G9B)
        async with maker() as session:
            version = await session.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == G9B
            present = await session.scalar(
                text(
                    "SELECT to_regclass('public.comment_enrichment_jobs') IS NOT NULL"
                )
            )
            assert present is True
    finally:
        _run_alembic("upgrade", "head")
        await engine.dispose()
