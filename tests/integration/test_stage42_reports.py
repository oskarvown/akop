"""Integration tests for Stage 4.2 CORE AuditArtifact + READY coupling."""
from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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


async def _claim_building_report(
    maker: async_sessionmaker[AsyncSession],
    *,
    report_date: dt.date,
    sha_prefix: str,
    valid_result: ValidationResult,
) -> tuple[int, object, object]:
    async with maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=report_date,
            sha_prefix=sha_prefix,
        )
        report = await session.scalar(
            select(AuditReport).where(AuditReport.audit_cycle_id == cycle_id)
        )
        assert report is not None
        report_id = report.id
        await session.commit()

    async with maker() as session:
        claim = await claim_report_build(
            session, report_id=report_id, settings=_settings()
        )
    assert claim is not None
    async with maker() as session:
        result = await run_claimed_build(session, claim=claim)
    return report_id, claim, result


@pytest.mark.asyncio
async def test_ready_requires_core_artifact(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    report_id, claim, result = await _claim_building_report(
        stage3_session_maker,
        report_date=dt.date(2026, 9, 1),
        sha_prefix="s42-core",
        valid_result=valid_result,
    )

    assert result.excel_bytes.startswith(b"PK")
    assert result.excel_sha256 == hashlib.sha256(result.excel_bytes).hexdigest()

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
async def test_empty_bytes_and_wrong_sha_leave_building(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    report_id, claim, result = await _claim_building_report(
        stage3_session_maker,
        report_date=dt.date(2026, 9, 3),
        sha_prefix="s42-bad",
        valid_result=valid_result,
    )

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

    with pytest.raises(ReportServiceError):
        async with stage3_session_maker() as session:
            await complete_report_build(
                session,
                report_id=report_id,
                claim_token=claim.claim_token,
                summary_json=result.summary_json,
                excel_bytes=result.excel_bytes,
                excel_sha256="0" * 64,
            )

    async with stage3_session_maker() as session:
        report = await session.get(AuditReport, report_id)
        assert report is not None
        assert report.status == AuditReportStatus.BUILDING
        assert report.build_claim_token == claim.claim_token
        assert report.summary_json is None
        assert report.built_at is None
        artifact_count = await session.scalar(
            select(AuditArtifact.id).where(AuditArtifact.audit_report_id == report_id)
        )
        assert artifact_count is None


@pytest.mark.asyncio
async def test_artifact_insert_failure_rolls_back_ready(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    report_id, claim, result = await _claim_building_report(
        stage3_session_maker,
        report_date=dt.date(2026, 9, 4),
        sha_prefix="s42-roll",
        valid_result=valid_result,
    )

    async with stage3_session_maker() as session:
        report = await session.get(AuditReport, report_id)
        assert report is not None
        assert report.status == AuditReportStatus.BUILDING
        input_hash = report.input_hash
        generator_version = report.generator_version
        schema_version = report.schema_version

    # Simulate complete_report_build success path with an invalid CORE revision.
    with pytest.raises(IntegrityError):
        async with stage3_session_maker() as session:
            async with session.begin():
                report = await session.scalar(
                    select(AuditReport)
                    .where(AuditReport.id == report_id)
                    .with_for_update()
                )
                assert report is not None
                assert report.build_claim_token == claim.claim_token
                session.add(
                    AuditArtifact(
                        audit_report_id=report_id,
                        kind=AuditArtifactKind.CORE,
                        revision=0,  # violates ck_audit_artifact_revision_ge_1
                        excel_bytes=result.excel_bytes,
                        excel_sha256=result.excel_sha256,
                        financial_input_hash=input_hash,
                        enrichment_input_hash=None,
                        generator_version=generator_version,
                        schema_version=schema_version,
                    )
                )
                report.status = AuditReportStatus.READY
                report.summary_json = result.summary_json
                report.built_at = dt.datetime.now(dt.timezone.utc)
                report.build_claim_token = None
                report.build_claimed_at = None
                await session.flush()

    async with stage3_session_maker() as session:
        report = await session.get(AuditReport, report_id)
        assert report is not None
        assert report.status == AuditReportStatus.BUILDING
        assert report.build_claim_token == claim.claim_token
        assert report.summary_json is None
        assert report.built_at is None
        assert (
            await session.scalar(
                select(AuditArtifact.id).where(
                    AuditArtifact.audit_report_id == report_id
                )
            )
            is None
        )

    # Ordinary failure flow can still move BUILDING → FAILED for retry.
    async with stage3_session_maker() as session:
        assert await fail_report_build(
            session,
            report_id=report_id,
            claim_token=claim.claim_token,
            error="artifact_insert_failed",
            settings=_settings(),
        )
    async with stage3_session_maker() as session:
        report = await session.get(AuditReport, report_id)
        assert report is not None
        assert report.status == AuditReportStatus.FAILED
        assert report.next_retry_at is not None


@pytest.mark.asyncio
async def test_duplicate_core_completion_rejected(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    report_id, claim, result = await _claim_building_report(
        stage3_session_maker,
        report_date=dt.date(2026, 9, 5),
        sha_prefix="s42-dup",
        valid_result=valid_result,
    )

    async with stage3_session_maker() as session:
        assert await complete_report_build(
            session,
            report_id=report_id,
            claim_token=claim.claim_token,
            summary_json=result.summary_json,
            excel_bytes=result.excel_bytes,
            excel_sha256=result.excel_sha256,
        )

    # Same token after READY: claim cleared → False, no second artifact.
    async with stage3_session_maker() as session:
        ok = await complete_report_build(
            session,
            report_id=report_id,
            claim_token=claim.claim_token,
            summary_json=result.summary_json,
            excel_bytes=result.excel_bytes,
            excel_sha256=result.excel_sha256,
        )
        assert ok is False

    # Force BUILDING again with a foreign token and attempt second CORE.
    foreign = uuid.uuid4()
    async with stage3_session_maker() as session:
        report = await session.get(AuditReport, report_id)
        assert report is not None
        report.status = AuditReportStatus.BUILDING
        report.build_claim_token = foreign
        report.build_claimed_at = dt.datetime.now(dt.timezone.utc)
        await session.commit()

    with pytest.raises(ReportServiceError, match="CORE artifact already exists"):
        async with stage3_session_maker() as session:
            await complete_report_build(
                session,
                report_id=report_id,
                claim_token=foreign,
                summary_json={"again": True},
                excel_bytes=result.excel_bytes + b"x",
                excel_sha256=hashlib.sha256(result.excel_bytes + b"x").hexdigest(),
            )

    async with stage3_session_maker() as session:
        artifacts = (
            await session.execute(
                select(AuditArtifact).where(
                    AuditArtifact.audit_report_id == report_id,
                    AuditArtifact.kind == AuditArtifactKind.CORE,
                )
            )
        ).scalars().all()
        assert len(artifacts) == 1
        assert artifacts[0].revision == 1
        report = await session.get(AuditReport, report_id)
        assert report is not None
        # Second txn rolled back → still BUILDING with foreign claim.
        assert report.status == AuditReportStatus.BUILDING
        assert report.build_claim_token == foreign


@pytest.mark.asyncio
async def test_rebuild_produces_identical_core_sha(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    report_id, claim, first = await _claim_building_report(
        stage3_session_maker,
        report_date=dt.date(2026, 9, 2),
        sha_prefix="s42-sha",
        valid_result=valid_result,
    )
    settings = _settings()

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


@pytest.mark.asyncio
async def test_core_revision_check_constraints(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    report_id, claim, result = await _claim_building_report(
        stage3_session_maker,
        report_date=dt.date(2026, 9, 6),
        sha_prefix="s42-ck",
        valid_result=valid_result,
    )
    async with stage3_session_maker() as session:
        assert await complete_report_build(
            session,
            report_id=report_id,
            claim_token=claim.claim_token,
            summary_json=result.summary_json,
            excel_bytes=result.excel_bytes,
            excel_sha256=result.excel_sha256,
        )

    async with stage3_session_maker() as session:
        report = await session.get(AuditReport, report_id)
        assert report is not None
        # CORE revision must stay 1.
        session.add(
            AuditArtifact(
                audit_report_id=report_id,
                kind=AuditArtifactKind.CORE,
                revision=2,
                excel_bytes=result.excel_bytes,
                excel_sha256=result.excel_sha256,
                financial_input_hash=report.input_hash,
                generator_version=report.generator_version,
                schema_version=report.schema_version,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
