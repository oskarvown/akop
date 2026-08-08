"""Integration tests for Stage 4.1 match keys, AuditReport enqueue, build claim."""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.audit_service import add_source_file_atomic
from app.application.comparison_service import compare_cycles
from app.application.report_service import (
    claim_report_build,
    complete_report_build,
    fail_report_build,
    recover_missing_reports,
    run_claimed_build,
)
from app.domain.enums import Department
from app.domain.models import (
    AuditCycle,
    AuditCycleStatus,
    AuditReport,
    AuditReportStatus,
    DebtPosition,
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


def _settings(**overrides: int) -> object:
    class S:
        report_build_claim_ttl_seconds = overrides.get("ttl", 300)
        report_build_max_attempts = overrides.get("max_attempts", 5)
        report_build_backoff_seconds = overrides.get("backoff", 60)

    return S()


@pytest.mark.asyncio
async def test_completed_creates_pending_audit_report(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    cycle_id = await complete_cycle(
        stage3_session,
        valid_result,
        report_date=dt.date(2026, 8, 1),
        sha_prefix="s41-pending",
    )
    report = await stage3_session.scalar(
        select(AuditReport).where(AuditReport.audit_cycle_id == cycle_id)
    )
    assert report is not None
    assert report.status == AuditReportStatus.PENDING
    assert report.previous_cycle_id is None
    assert report.generator_version
    assert report.schema_version
    assert len(report.input_hash) == 64


@pytest.mark.asyncio
async def test_match_keys_persisted_and_backfill_shape(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    await complete_cycle(
        stage3_session,
        valid_result,
        report_date=dt.date(2026, 8, 2),
        sha_prefix="s41-keys",
    )
    positions = (
        await stage3_session.execute(select(DebtPosition).order_by(DebtPosition.id))
    ).scalars().all()
    assert positions
    for position in positions:
        assert position.normalized_label
        assert position.match_key
        assert len(position.match_key_hash) == 64
        if position.outline_level == 1:
            assert position.match_key == f"c:{position.counterparty_id}"
        else:
            assert f"|{position.outline_level}:" in position.match_key


@pytest.mark.asyncio
async def test_previous_completed_linked_on_second_cycle(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    first_id = await complete_cycle(
        stage3_session,
        valid_result,
        report_date=dt.date(2026, 8, 6),
        sha_prefix="s41-prev-a",
    )
    second_id = await complete_cycle(
        stage3_session,
        valid_result,
        report_date=dt.date(2026, 8, 13),
        sha_prefix="s41-prev-b",
    )
    report = await stage3_session.scalar(
        select(AuditReport).where(AuditReport.audit_cycle_id == second_id)
    )
    assert report is not None
    assert report.previous_cycle_id == first_id

    cycle = await stage3_session.scalar(
        select(AuditCycle).where(AuditCycle.id == second_id)
    )
    assert cycle is not None
    comparison = await compare_cycles(stage3_session, current_cycle=cycle)
    assert comparison.previous_cycle_id == first_id
    assert comparison.entities


@pytest.mark.asyncio
async def test_recover_missing_report(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async with stage3_session_maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 8, 3),
            sha_prefix="s41-recover",
        )
        await session.execute(
            delete(AuditReport).where(AuditReport.audit_cycle_id == cycle_id)
        )
        await session.commit()

    created = await recover_missing_reports(stage3_session_maker())
    assert created
    async with stage3_session_maker() as session:
        report = await session.scalar(
            select(AuditReport).where(AuditReport.audit_cycle_id == cycle_id)
        )
        assert report is not None
        assert report.status == AuditReportStatus.PENDING


@pytest.mark.asyncio
async def test_build_claim_single_winner_and_complete(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async with stage3_session_maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 8, 4),
            sha_prefix="s41-claim",
        )
        report = await session.scalar(
            select(AuditReport).where(AuditReport.audit_cycle_id == cycle_id)
        )
        assert report is not None
        report_id = report.id
        await session.commit()

    settings = _settings()
    async with stage3_session_maker() as session:
        claim_a = await claim_report_build(
            session, report_id=report_id, settings=settings
        )
    async with stage3_session_maker() as session:
        claim_b = await claim_report_build(
            session, report_id=report_id, settings=settings
        )
    assert claim_a is not None
    assert claim_b is None

    async with stage3_session_maker() as session:
        summary = await run_claimed_build(session, claim=claim_a)
    async with stage3_session_maker() as session:
        ok = await complete_report_build(
            session,
            report_id=report_id,
            claim_token=claim_a.claim_token,
            summary_json=summary,
        )
        assert ok is True
    async with stage3_session_maker() as session:
        report = await session.get(AuditReport, report_id)
        assert report is not None
        assert report.status == AuditReportStatus.READY
        assert report.summary_json is not None
        assert report.build_claim_token is None


@pytest.mark.asyncio
async def test_build_fail_retry_and_max_attempts(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async with stage3_session_maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 8, 5),
            sha_prefix="s41-fail",
        )
        report = await session.scalar(
            select(AuditReport).where(AuditReport.audit_cycle_id == cycle_id)
        )
        assert report is not None
        report_id = report.id
        await session.commit()

    settings = _settings(max_attempts=2, backoff=1)
    # Attempt 1
    async with stage3_session_maker() as session:
        claim = await claim_report_build(
            session, report_id=report_id, settings=settings
        )
    assert claim is not None
    async with stage3_session_maker() as session:
        assert await fail_report_build(
            session,
            report_id=report_id,
            claim_token=claim.claim_token,
            error="boom",
            settings=settings,
        )

    # Attempt 2 (due immediately with next_retry_at in past/now)
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
        await fail_report_build(
            session,
            report_id=report_id,
            claim_token=claim2.claim_token,
            error="boom2",
            settings=settings,
        )

    async with stage3_session_maker() as session:
        claim3 = await claim_report_build(
            session, report_id=report_id, settings=settings
        )
    assert claim3 is None
    async with stage3_session_maker() as session:
        report = await session.get(AuditReport, report_id)
        assert report is not None
        assert report.status == AuditReportStatus.FAILED
        assert report.build_attempt_count >= 2


@pytest.mark.asyncio
async def test_build_ttl_reclaim(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async with stage3_session_maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 8, 7),
            sha_prefix="s41-ttl",
        )
        report = await session.scalar(
            select(AuditReport).where(AuditReport.audit_cycle_id == cycle_id)
        )
        assert report is not None
        report_id = report.id
        await session.commit()

    settings = _settings(ttl=1)
    async with stage3_session_maker() as session:
        claim = await claim_report_build(
            session, report_id=report_id, settings=settings
        )
    assert claim is not None

    # Simulate stale claim
    async with stage3_session_maker() as session:
        report = await session.get(AuditReport, report_id)
        assert report is not None
        report.build_claimed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            seconds=10
        )
        await session.commit()

    # First call reclaims to FAILED; second can claim again
    async with stage3_session_maker() as session:
        again = await claim_report_build(
            session, report_id=report_id, settings=settings
        )
    assert again is None
    async with stage3_session_maker() as session:
        report = await session.get(AuditReport, report_id)
        assert report is not None
        assert report.status == AuditReportStatus.FAILED
        report.next_retry_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
        await session.commit()

    async with stage3_session_maker() as session:
        reclaim_claim = await claim_report_build(
            session, report_id=report_id, settings=settings
        )
    assert reclaim_claim is not None
    assert reclaim_claim.claim_token != claim.claim_token


@pytest.mark.asyncio
async def test_late_completed_previous_cycle_immutable(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    """Aug-20 report freezes previous=Aug-06 even if Aug-13 completes later."""
    aug6 = await complete_cycle(
        stage3_session,
        valid_result,
        report_date=dt.date(2026, 8, 6),
        sha_prefix="s41-late-a",
    )
    aug20 = await complete_cycle(
        stage3_session,
        valid_result,
        report_date=dt.date(2026, 8, 20),
        sha_prefix="s41-late-b",
    )
    report20 = await stage3_session.scalar(
        select(AuditReport).where(AuditReport.audit_cycle_id == aug20)
    )
    assert report20 is not None
    assert report20.previous_cycle_id == aug6

    # Mark READY to lock snapshot semantics for Stage 4.1
    report20.status = AuditReportStatus.READY
    report20.summary_json = {"frozen": True}
    await stage3_session.commit()

    aug13 = await complete_cycle(
        stage3_session,
        valid_result,
        report_date=dt.date(2026, 8, 13),
        sha_prefix="s41-late-c",
    )
    await stage3_session.refresh(report20)
    assert report20.previous_cycle_id == aug6

    report13 = await stage3_session.scalar(
        select(AuditReport).where(AuditReport.audit_cycle_id == aug13)
    )
    assert report13 is not None
    assert report13.previous_cycle_id == aug6


@pytest.mark.asyncio
async def test_complete_rejects_wrong_token(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async with stage3_session_maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 8, 8),
            sha_prefix="s41-token",
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
        ok = await complete_report_build(
            session,
            report_id=report_id,
            claim_token=uuid.uuid4(),
            summary_json={"x": 1},
        )
    assert ok is False
