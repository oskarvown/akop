"""Real Alembic upgrade backfill for Stage 4.1 match_key columns.

Runs serially against the shared test DB: downgrades to c5e300000001, inserts
pre-Stage-4.1 rows via raw SQL (no match_key columns), upgrades through
d6e400000001, then restores head. Marked ``alembic_migration`` so it can be
excluded from parallel/default suites that share the same database.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import unicodedata
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
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_label(raw_name: str) -> str:
    text_value = unicodedata.normalize("NFKC", raw_name)
    text_value = text_value.strip()
    text_value = _WHITESPACE_RE.sub(" ", text_value)
    return text_value.casefold()


def _match_key_hash(match_key: str) -> str:
    return hashlib.sha256(match_key.encode("utf-8")).hexdigest()


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
async def test_alembic_d6e400000001_backfills_existing_debt_positions() -> None:
    settings = get_settings()
    assert_destructive_cleanup_allowed(settings.db_name)

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    maker: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=engine, expire_on_commit=True
    )

    try:
        await clean_stage3_tables(maker, db_name=settings.db_name)

        # Ensure we start from head, then go to pre-match_key revision.
        _run_alembic("upgrade", "head")
        _run_alembic("downgrade", "c5e300000001")

        async with maker() as session:
            # Columns must not exist yet on c5e300000001.
            cols = (
                await session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'debt_positions' "
                        "AND column_name IN "
                        "('normalized_label', 'match_key', 'match_key_hash')"
                    )
                )
            ).scalars().all()
            assert list(cols) == []

            # Pre-Stage-4.1 hierarchy via raw SQL (old schema only).
            cycle_id = await session.scalar(
                text(
                    "INSERT INTO audit_cycles "
                    "(report_date, status, notification_chat_id, reminder_count) "
                    "VALUES ('2026-08-15', 'completed', 743971617, 0) "
                    "RETURNING id"
                )
            )
            mg_id = await session.scalar(
                text(
                    "INSERT INTO manager_groups "
                    "(department, raw_name, normalized_name) "
                    "VALUES ('REGIONAL', 'Менеджер А', 'менеджер а') "
                    "RETURNING id"
                )
            )
            cp_id = await session.scalar(
                text(
                    "INSERT INTO counterparties "
                    "(manager_group_id, raw_name, normalized_name) "
                    "VALUES (:mg, 'ООО Ромашка', 'ооо ромашка') "
                    "RETURNING id"
                ),
                {"mg": mg_id},
            )
            sf_id = await session.scalar(
                text(
                    "INSERT INTO source_files "
                    "(audit_cycle_id, department, report_date, sha256, "
                    " original_filename, fingerprint_name, status, "
                    " lifecycle_status) "
                    "VALUES "
                    "(:cycle, 'REGIONAL', '2026-08-15', "
                    " 'sha-alembic-backfill-1', 'legacy.xls', "
                    " 'regional_v1', 'VALID', 'active') "
                    "RETURNING id"
                ),
                {"cycle": cycle_id},
            )

            l1_id = await session.scalar(
                text(
                    "INSERT INTO debt_positions "
                    "(source_file_id, manager_group_id, counterparty_id, "
                    " parent_position_id, outline_level, row_order, raw_label, "
                    " total_debt) "
                    "VALUES "
                    "(:sf, :mg, :cp, NULL, 1, 10, 'ООО Ромашка', 100.00) "
                    "RETURNING id"
                ),
                {"sf": sf_id, "mg": mg_id, "cp": cp_id},
            )
            l2_id = await session.scalar(
                text(
                    "INSERT INTO debt_positions "
                    "(source_file_id, manager_group_id, counterparty_id, "
                    " parent_position_id, outline_level, row_order, raw_label, "
                    " total_debt) "
                    "VALUES "
                    "(:sf, :mg, :cp, :parent, 2, 11, 'Договор № 1', 100.00) "
                    "RETURNING id"
                ),
                {"sf": sf_id, "mg": mg_id, "cp": cp_id, "parent": l1_id},
            )
            l3_id = await session.scalar(
                text(
                    "INSERT INTO debt_positions "
                    "(source_file_id, manager_group_id, counterparty_id, "
                    " parent_position_id, outline_level, row_order, raw_label, "
                    " total_debt) "
                    "VALUES "
                    "(:sf, :mg, :cp, :parent, 3, 12, 'Объект А', 100.00) "
                    "RETURNING id"
                ),
                {"sf": sf_id, "mg": mg_id, "cp": cp_id, "parent": l2_id},
            )
            l4_id = await session.scalar(
                text(
                    "INSERT INTO debt_positions "
                    "(source_file_id, manager_group_id, counterparty_id, "
                    " parent_position_id, outline_level, row_order, raw_label, "
                    " total_debt) "
                    "VALUES "
                    "(:sf, :mg, :cp, :parent, 4, 13, 'УПД-7', 100.00) "
                    "RETURNING id"
                ),
                {"sf": sf_id, "mg": mg_id, "cp": cp_id, "parent": l3_id},
            )
            await session.commit()

        # Real Alembic path — not a helper call.
        _run_alembic("upgrade", "d6e400000001")

        async with maker() as session:
            version = await session.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            assert version == "d6e400000001"

            nullables = (
                await session.execute(
                    text(
                        "SELECT column_name, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_name = 'debt_positions' "
                        "AND column_name IN "
                        "('normalized_label', 'match_key', 'match_key_hash') "
                        "ORDER BY column_name"
                    )
                )
            ).all()
            assert {row[0]: row[1] for row in nullables} == {
                "match_key": "NO",
                "match_key_hash": "NO",
                "normalized_label": "NO",
            }

            indexes = (
                await session.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE tablename = 'debt_positions' "
                        "AND indexname IN "
                        "('ix_debt_positions_match_key', "
                        " 'ix_debt_positions_match_key_hash') "
                        "ORDER BY indexname"
                    )
                )
            ).scalars().all()
            assert list(indexes) == [
                "ix_debt_positions_match_key",
                "ix_debt_positions_match_key_hash",
            ]

            audit_reports_exists = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_name = 'audit_reports'"
                )
            )
            assert int(audit_reports_exists or 0) == 1

            rows = (
                await session.execute(
                    text(
                        "SELECT id, outline_level, counterparty_id, raw_label, "
                        "normalized_label, match_key, match_key_hash "
                        "FROM debt_positions ORDER BY outline_level, id"
                    )
                )
            ).all()
            assert len(rows) == 4

            expected_by_level: dict[int, str] = {}
            for (
                position_id,
                outline_level,
                counterparty_id,
                raw_label,
                normalized_label,
                match_key,
                key_hash,
            ) in rows:
                expected_norm = _normalize_label(str(raw_label))
                assert normalized_label == expected_norm
                if outline_level == 1:
                    expected_key = f"c:{counterparty_id}"
                else:
                    parent_key = expected_by_level[outline_level - 1]
                    expected_key = (
                        f"{parent_key}|{outline_level}:{expected_norm}"
                    )
                expected_by_level[int(outline_level)] = expected_key
                assert match_key == expected_key
                assert key_hash == _match_key_hash(expected_key)
                assert position_id in {l1_id, l2_id, l3_id, l4_id}
    finally:
        try:
            _run_alembic("upgrade", "head")
        finally:
            await clean_stage3_tables(maker, db_name=settings.db_name)
            await engine.dispose()
