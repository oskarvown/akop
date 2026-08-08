"""Integration tests for Stage 4.2 CORE AuditArtifact + READY coupling."""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.audit_service import add_source_file_atomic
from app.application.report_service import (
    ReportServiceError,
    claim_report_build,
    complete_report_build,
    fail_report_build,
    run_claimed_build,
)
from app.domain.enums import Department
from app.domain.models import (
    AuditArtifact,
    AuditArtifactKind,
    AuditCycleStatus,
    AuditReport,
    AuditReportStatus,
)
from app.infrastructure.excel.validator import (
    ValidationResult,
    validate_confirmed_template_file,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "regional"
    / "regional_valid_basic.xls"
)
NOTIFY_CHAT_ID = 743971617


@pytest.fixture
def valid_result() -> ValidationResult:
    result = validate_confirmed_template_file(FIXTURE)
    assert result.is_valid and result.parsed is not None
    return result


def result_for_date(result: ValidationResult, report_date: dt.date) -> ValidationResult:
    assert result.parsed is not None
    return replace(result, parsed=replace(result.parsed, report_date=report_date))


async def complete_cycle(
    session: AsyncSession,
    result: ValidationResult,
    *,
    report_date: dt.date,
    sha_prefix: str,
) -> int:
    dated = result_for_date(result, report_date)
    last = None
    for index, department in enumerate(Department):
        last = await add_source_file_atomic(
            session,
            result=dated,
            department=department,
            sha256=f"{sha_prefix}-{index}",
            original_filename=f"{sha_prefix}-{index}.xls",
            report_date=report_date,
            notification_chat_id=NOTIFY_CHAT_ID,
        )
    assert last is not None
    assert last.status == AuditCycleStatus.COMPLETED
    return last.cycle_id


def _settings() -> object:
    class S:
        report_build_claim_ttl_seconds = 300
        report_build_max_attempts = 5
        report_build_backoff_seconds = 1

    return S()


@pytest.mark.asyncio
async def test_ready_requires_core_artifact(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async with stage3_session_maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 9, 1),
            sha_prefix="s42-core",
        )
        report = await session.scalar(
            select(AuditReport).where(AuditReport.audit_cycle_id == cycle_id)
        )
        assert report is not None
        report_id = report.id
        await session.commit()

    async with stage3_session_maker() as session:
        claim = await claim_report_build(
            session, report_id=report_id, settings=_settings()
        )
    assert claim is not None

    async with stage3_session_maker() as session:
        result = await run_claimed_build(session, claim=claim)

    assert result.excel_bytes.startswith(b"PK")
    assert result.excel_sha256 == hashlib.sha256(result.excel_bytes).hexdigest()

    with pytest.raises(ReportServiceError):
        async with stage3_session_maker() as session:
            await complete_report_build(
                session,
                report_id=report_id,
                claim_token=claim.claim_token,
                summary_json=result.summary_json,
                excel_bytes=b"",
                excel_sha256=result.excel_sha256,
            )

    async with stage3_session_maker() as session:
        ok = await complete_report_build(
            session,
            report_id=report_id,
            claim_token=claim.claim_token,
            summary_json=result.summary_json,
            excel_bytes=result.excel_bytes,
            excel_sha256=result.excel_sha256,
        )
        assert ok is True

    async with stage3_session_maker() as session:
        report = await session.get(AuditReport, report_id)
        assert report is not None
        assert report.status == AuditReportStatus.READY
        artifact = await session.scalar(
            select(AuditArtifact).where(
                AuditArtifact.audit_report_id == report_id,
                AuditArtifact.kind == AuditArtifactKind.CORE,
                AuditArtifact.revision == 1,
            )
        )
        assert artifact is not None
        assert artifact.excel_bytes == result.excel_bytes
        assert artifact.excel_sha256 == result.excel_sha256
        assert artifact.financial_input_hash == report.input_hash
        assert artifact.generator_version == report.generator_version


@pytest.mark.asyncio
async def test_rebuild_produces_identical_core_sha(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async with stage3_session_maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 9, 2),
            sha_prefix="s42-sha",
        )
        report = await session.scalar(
            select(AuditReport).where(AuditReport.audit_cycle_id == cycle_id)
        )
        assert report is not None
        report_id = report.id
        await session.commit()

    settings = _settings()
    async with stage3_session_maker() as session:
        claim = await claim_report_build(
            session, report_id=report_id, settings=settings
        )
    assert claim is not None
    async with stage3_session_maker() as session:
        first = await run_claimed_build(session, claim=claim)

    async with stage3_session_maker() as session:
        await fail_report_build(
            session,
            report_id=report_id,
            claim_token=claim.claim_token,
            error="retry for sha",
            settings=settings,
        )
    async with stage3_session_maker() as session:
        report = await session.get(AuditReport, report_id)
        assert report is not None
        report.next_retry_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
        await session.commit()

    async with stage3_session_maker() as session:
        claim2 = await claim_report_build(
            session, report_id=report_id, settings=settings
        )
    assert claim2 is not None
    async with stage3_session_maker() as session:
        second = await run_claimed_build(session, claim=claim2)

    assert first.excel_sha256 == second.excel_sha256
    assert first.excel_bytes == second.excel_bytes
