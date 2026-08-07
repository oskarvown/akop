from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.application.audit_service as audit_service
from app.application.audit_service import (
    AuditCycleNotFoundError,
    CycleImmutableError,
    DepartmentSlotTakenError,
    DuplicateSourceFileError,
    StaleReplacementError,
    add_source_file_atomic,
    count_collecting_cycles,
    find_audit_cycle_by_report_date,
    find_source_file_by_sha256,
    get_active_source_file,
    replace_source_file_atomic,
)
from app.domain.enums import Department
from app.domain.models import (
    AuditCycle,
    AuditCycleStatus,
    SourceFile,
    SourceFileLifecycle,
    SourceFileStatus,
)
from app.infrastructure.excel.validator import ValidationResult, validate_confirmed_template_file

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "regional"
    / "regional_valid_basic.xls"
)
REPORT_DATE = dt.date(2026, 7, 30)


@pytest.fixture
def valid_result() -> ValidationResult:
    result = validate_confirmed_template_file(FIXTURE)
    assert result.is_valid and result.parsed is not None
    return result


def result_for_date(result: ValidationResult, report_date: dt.date) -> ValidationResult:
    assert result.parsed is not None
    return replace(result, parsed=replace(result.parsed, report_date=report_date))


async def add(
    session: AsyncSession,
    result: ValidationResult,
    department: Department,
    *,
    sha: str,
    report_date: dt.date = REPORT_DATE,
):
    dated = result_for_date(result, report_date)
    return await add_source_file_atomic(
        session,
        result=dated,
        department=department,
        sha256=sha,
        original_filename=f"{sha}.xls",
        report_date=report_date,
    )


@pytest.mark.asyncio
async def test_full_set_completes_only_at_five_of_five(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    departments = list(Department)
    for index, department in enumerate(departments[:4]):
        result = await add(
            stage3_session,
            valid_result,
            department,
            sha=f"full-{index}",
        )
        assert result.status == AuditCycleStatus.COLLECTING
        assert len(result.summary.present) == index + 1

    result = await add(
        stage3_session,
        valid_result,
        departments[4],
        sha="full-4",
    )
    assert result.status == AuditCycleStatus.COMPLETED
    assert result.summary.is_complete

    async with stage3_session.begin():
        cycle = await stage3_session.scalar(select(AuditCycle))
        assert cycle is not None
        assert cycle.status == AuditCycleStatus.COMPLETED
        assert cycle.completed_at is not None


@pytest.mark.asyncio
async def test_concurrent_fourth_and_fifth_files_complete_cycle(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    departments = list(Department)
    for index, department in enumerate(departments[:3]):
        async with stage3_session_maker() as session:
            await add(session, valid_result, department, sha=f"seed-{index}")

    async def concurrent_add(department: Department, sha: str) -> None:
        async with stage3_session_maker() as session:
            await add(session, valid_result, department, sha=sha)

    await asyncio.gather(
        concurrent_add(departments[3], "parallel-four"),
        concurrent_add(departments[4], "parallel-five"),
    )

    async with stage3_session_maker() as session, session.begin():
        cycle = await session.scalar(select(AuditCycle))
        files = (
            await session.scalars(
                select(SourceFile).where(
                    SourceFile.lifecycle_status == SourceFileLifecycle.ACTIVE
                )
            )
        ).all()
        assert cycle is not None
        assert cycle.status == AuditCycleStatus.COMPLETED
        assert len(files) == 5


@pytest.mark.asyncio
async def test_concurrent_cycle_creation_creates_exactly_one_cycle(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async def concurrent_add(department: Department, sha: str) -> None:
        async with stage3_session_maker() as session:
            await add(session, valid_result, department, sha=sha)

    await asyncio.gather(
        concurrent_add(Department.REGIONAL, "cycle-race-a"),
        concurrent_add(Department.MOSCOW, "cycle-race-b"),
    )

    async with stage3_session_maker() as session, session.begin():
        assert (
            await session.scalar(
                select(func.count(AuditCycle.id)).where(
                    AuditCycle.report_date == REPORT_DATE
                )
            )
            == 1
        )
        assert await session.scalar(select(func.count(SourceFile.id))) == 2


@pytest.mark.asyncio
async def test_concurrent_duplicate_sha_creates_one_source_file(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async def concurrent_add(department: Department):
        async with stage3_session_maker() as session:
            try:
                await add(session, valid_result, department, sha="same-sha")
                return "ok"
            except DuplicateSourceFileError:
                return "duplicate"

    outcomes = await asyncio.gather(
        concurrent_add(Department.REGIONAL),
        concurrent_add(Department.MOSCOW),
    )
    assert sorted(outcomes) == ["duplicate", "ok"]
    async with stage3_session_maker() as session, session.begin():
        assert await session.scalar(select(func.count(SourceFile.id))) == 1


@pytest.mark.asyncio
async def test_replace_preserves_history_and_rejects_stale_confirmation(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    first = await add(
        stage3_session,
        valid_result,
        Department.REGIONAL,
        sha="replace-a",
    )
    second = await replace_source_file_atomic(
        stage3_session,
        result=valid_result,
        department=Department.REGIONAL,
        sha256="replace-b",
        original_filename="replace-b.xls",
        report_date=REPORT_DATE,
        expected_active_source_file_id=first.source_file_id,
    )

    with pytest.raises(StaleReplacementError):
        await replace_source_file_atomic(
            stage3_session,
            result=valid_result,
            department=Department.REGIONAL,
            sha256="replace-c",
            original_filename="replace-c.xls",
            report_date=REPORT_DATE,
            expected_active_source_file_id=first.source_file_id,
        )

    async with stage3_session.begin():
        files = (
            await stage3_session.scalars(
                select(SourceFile).order_by(SourceFile.id)
            )
        ).all()
        assert [item.lifecycle_status for item in files] == [
            SourceFileLifecycle.SUPERSEDED,
            SourceFileLifecycle.ACTIVE,
        ]
        assert files[1].id == second.source_file_id
        assert len(files) == 2


@pytest.mark.asyncio
async def test_replace_rolls_back_supersede_when_insert_fails(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await add(
        stage3_session,
        valid_result,
        Department.REGIONAL,
        sha="rollback-a",
    )

    async def fail_persist(*args, **kwargs):
        raise RuntimeError("forced failure after supersede")

    monkeypatch.setattr(audit_service, "persist_valid_source_file", fail_persist)
    with pytest.raises(RuntimeError, match="forced failure"):
        await replace_source_file_atomic(
            stage3_session,
            result=valid_result,
            department=Department.REGIONAL,
            sha256="rollback-b",
            original_filename="rollback-b.xls",
            report_date=REPORT_DATE,
            expected_active_source_file_id=first.source_file_id,
        )

    async with stage3_session.begin():
        files = (await stage3_session.scalars(select(SourceFile))).all()
        assert len(files) == 1
        assert files[0].lifecycle_status == SourceFileLifecycle.ACTIVE


@pytest.mark.asyncio
async def test_superseded_sha_remains_globally_duplicate(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    first = await add(
        stage3_session,
        valid_result,
        Department.REGIONAL,
        sha="historical-sha",
    )
    await replace_source_file_atomic(
        stage3_session,
        result=valid_result,
        department=Department.REGIONAL,
        sha256="current-sha",
        original_filename="current.xls",
        report_date=REPORT_DATE,
        expected_active_source_file_id=first.source_file_id,
    )
    with pytest.raises(DuplicateSourceFileError):
        await add(
            stage3_session,
            valid_result,
            Department.MOSCOW,
            sha="historical-sha",
        )


@pytest.mark.asyncio
async def test_different_dates_create_separate_cycles(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    await add(
        stage3_session,
        valid_result,
        Department.REGIONAL,
        sha="date-a",
        report_date=REPORT_DATE,
    )
    other_date = REPORT_DATE + dt.timedelta(days=7)
    await add(
        stage3_session,
        valid_result,
        Department.REGIONAL,
        sha="date-b",
        report_date=other_date,
    )
    async with stage3_session.begin():
        dates = set((await stage3_session.scalars(select(AuditCycle.report_date))).all())
        assert dates == {REPORT_DATE, other_date}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [AuditCycleStatus.COMPLETED, AuditCycleStatus.EXPIRED],
)
async def test_add_and_replace_reject_immutable_cycles(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
    status: AuditCycleStatus,
) -> None:
    async with stage3_session.begin():
        cycle = AuditCycle(report_date=REPORT_DATE, status=status)
        stage3_session.add(cycle)

    with pytest.raises(CycleImmutableError):
        await add(
            stage3_session,
            valid_result,
            Department.REGIONAL,
            sha=f"immutable-add-{status.value}",
        )
    with pytest.raises(CycleImmutableError):
        await replace_source_file_atomic(
            stage3_session,
            result=valid_result,
            department=Department.REGIONAL,
            sha256=f"immutable-replace-{status.value}",
            original_filename="immutable.xls",
            report_date=REPORT_DATE,
            expected_active_source_file_id=999,
        )


@pytest.mark.asyncio
async def test_replace_never_creates_missing_cycle(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    with pytest.raises(AuditCycleNotFoundError):
        await replace_source_file_atomic(
            stage3_session,
            result=valid_result,
            department=Department.REGIONAL,
            sha256="missing-cycle",
            original_filename="missing.xls",
            report_date=REPORT_DATE,
            expected_active_source_file_id=1,
        )
    async with stage3_session.begin():
        assert await stage3_session.scalar(select(func.count(AuditCycle.id))) == 0


@pytest.mark.asyncio
async def test_last_activity_changes_after_add_and_replace(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    first = await add(
        stage3_session,
        valid_result,
        Department.REGIONAL,
        sha="activity-a",
    )
    async with stage3_session.begin():
        before = await stage3_session.scalar(
            select(AuditCycle.last_activity_at).where(AuditCycle.id == first.cycle_id)
        )
    await asyncio.sleep(0.01)
    await replace_source_file_atomic(
        stage3_session,
        result=valid_result,
        department=Department.REGIONAL,
        sha256="activity-b",
        original_filename="activity-b.xls",
        report_date=REPORT_DATE,
        expected_active_source_file_id=first.source_file_id,
    )
    async with stage3_session.begin():
        after = await stage3_session.scalar(
            select(AuditCycle.last_activity_at).where(AuditCycle.id == first.cycle_id)
        )
    assert before is not None and after is not None
    assert after > before


@pytest.mark.asyncio
async def test_partial_unique_index_rejects_two_active_department_files(
    stage3_session: AsyncSession,
) -> None:
    with pytest.raises(IntegrityError):
        async with stage3_session.begin():
            cycle = AuditCycle(report_date=REPORT_DATE)
            stage3_session.add(cycle)
            await stage3_session.flush()
            stage3_session.add_all(
                [
                    _minimal_source("index-a", cycle.id, SourceFileLifecycle.ACTIVE),
                    _minimal_source("index-b", cycle.id, SourceFileLifecycle.ACTIVE),
                ]
            )
            await stage3_session.flush()


@pytest.mark.asyncio
async def test_read_dtos_are_safe_with_expire_on_commit_and_legacy_rows(
    stage3_session: AsyncSession,
) -> None:
    async with stage3_session.begin():
        cycle = AuditCycle(report_date=REPORT_DATE)
        stage3_session.add(cycle)
        await stage3_session.flush()
        active = _minimal_source("lookup-active", cycle.id, SourceFileLifecycle.ACTIVE)
        superseded = _minimal_source(
            "lookup-superseded",
            cycle.id,
            SourceFileLifecycle.SUPERSEDED,
            Department.MOSCOW,
        )
        legacy = _minimal_source(
            "lookup-legacy",
            None,
            SourceFileLifecycle.ACTIVE,
            Department.SZFO_1,
        )
        stage3_session.add_all([active, superseded, legacy])

    for sha, lifecycle, cycle_expected in (
        ("lookup-active", SourceFileLifecycle.ACTIVE, True),
        ("lookup-superseded", SourceFileLifecycle.SUPERSEDED, True),
        ("lookup-legacy", SourceFileLifecycle.ACTIVE, False),
    ):
        dto = await find_source_file_by_sha256(stage3_session, sha)
        assert dto is not None
        assert dto.id > 0
        assert dto.report_date == REPORT_DATE
        assert isinstance(dto.department, Department)
        assert dto.lifecycle_status == lifecycle
        assert (dto.audit_cycle_id is not None) is cycle_expected
        assert stage3_session.in_transaction() is False

    cycle_dto = await find_audit_cycle_by_report_date(stage3_session, REPORT_DATE)
    assert cycle_dto is not None
    assert cycle_dto.id > 0
    assert cycle_dto.report_date == REPORT_DATE
    assert cycle_dto.status == AuditCycleStatus.COLLECTING
    assert stage3_session.in_transaction() is False

    collecting = await count_collecting_cycles(stage3_session)
    assert collecting and collecting[0].report_date == REPORT_DATE
    assert stage3_session.in_transaction() is False

    active_dto = await get_active_source_file(
        stage3_session, cycle_dto.id, Department.REGIONAL
    )
    assert active_dto is not None
    assert active_dto.id > 0
    assert active_dto.original_filename == "lookup-active.xls"
    assert active_dto.total_debt is not None
    assert stage3_session.in_transaction() is False


@pytest.mark.asyncio
async def test_handler_style_lookup_then_add_and_replace_has_clean_transaction_boundary(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    assert await find_source_file_by_sha256(stage3_session, "handler-a") is None
    assert await find_audit_cycle_by_report_date(stage3_session, REPORT_DATE) is None
    assert await count_collecting_cycles(stage3_session) == []
    assert stage3_session.in_transaction() is False
    first = await add(
        stage3_session,
        valid_result,
        Department.REGIONAL,
        sha="handler-a",
    )

    cycle = await find_audit_cycle_by_report_date(stage3_session, REPORT_DATE)
    assert cycle is not None
    active = await get_active_source_file(
        stage3_session, cycle.id, Department.REGIONAL
    )
    assert active is not None
    assert stage3_session.in_transaction() is False
    await replace_source_file_atomic(
        stage3_session,
        result=valid_result,
        department=Department.REGIONAL,
        sha256="handler-b",
        original_filename="handler-b.xls",
        report_date=REPORT_DATE,
        expected_active_source_file_id=first.source_file_id,
    )


@pytest.mark.asyncio
async def test_enum_values_roundtrip_through_orm(
    stage3_session: AsyncSession,
) -> None:
    async with stage3_session.begin():
        cycles = [
            AuditCycle(
                report_date=REPORT_DATE + dt.timedelta(days=index),
                status=status,
            )
            for index, status in enumerate(AuditCycleStatus)
        ]
        stage3_session.add_all(cycles)
        stage3_session.add_all(
            [
                _minimal_source(
                    "enum-active",
                    None,
                    SourceFileLifecycle.ACTIVE,
                ),
                _minimal_source(
                    "enum-superseded",
                    None,
                    SourceFileLifecycle.SUPERSEDED,
                    Department.MOSCOW,
                ),
            ]
        )

    async with stage3_session.begin():
        statuses = set((await stage3_session.scalars(select(AuditCycle.status))).all())
        lifecycles = set(
            (await stage3_session.scalars(select(SourceFile.lifecycle_status))).all()
        )
        assert statuses == set(AuditCycleStatus)
        assert lifecycles == set(SourceFileLifecycle)


@pytest.mark.asyncio
async def test_integrity_error_retry_opens_new_transaction_and_keeps_session_usable(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    begin_count = {"n": 0}
    real_begin = stage3_session.begin
    real_get_or_create = audit_service.get_or_create_audit_cycle

    def counting_begin(*args, **kwargs):
        begin_count["n"] += 1
        return real_begin(*args, **kwargs)

    async def flaky_get_or_create(session, report_date):
        if begin_count["n"] == 1:
            class FakeOrig(Exception):
                pass

            raise IntegrityError(
                "INSERT",
                {},
                FakeOrig(
                    'duplicate key value violates unique constraint '
                    '"uq_audit_cycle_report_date"'
                ),
            )
        return await real_get_or_create(session, report_date)

    monkeypatch.setattr(stage3_session, "begin", counting_begin)
    monkeypatch.setattr(audit_service, "get_or_create_audit_cycle", flaky_get_or_create)

    result = await add(
        stage3_session,
        valid_result,
        Department.REGIONAL,
        sha="retry-tx-1",
    )
    assert begin_count["n"] == 2
    assert stage3_session.in_transaction() is False
    assert result.cycle_id > 0

    # After translated IntegrityError the same session remains writable.
    with pytest.raises(DuplicateSourceFileError):
        await add(
            stage3_session,
            valid_result,
            Department.MOSCOW,
            sha="retry-tx-1",
        )
    assert stage3_session.in_transaction() is False
    second = await add(
        stage3_session,
        valid_result,
        Department.MOSCOW,
        sha="retry-tx-2",
    )
    assert second.cycle_id == result.cycle_id


@pytest.mark.asyncio
async def test_replace_never_commits_superseded_without_new_active_file(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await add(
        stage3_session,
        valid_result,
        Department.REGIONAL,
        sha="atomic-order-old",
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("forced failure before new file commit")

    monkeypatch.setattr(audit_service, "persist_valid_source_file", boom)
    with pytest.raises(RuntimeError, match="forced failure"):
        await replace_source_file_atomic(
            stage3_session,
            result=valid_result,
            department=Department.REGIONAL,
            sha256="atomic-order-new",
            original_filename="atomic-order-new.xls",
            report_date=REPORT_DATE,
            expected_active_source_file_id=first.source_file_id,
        )

    async with stage3_session.begin():
        rows = (
            await stage3_session.execute(
                select(SourceFile.sha256, SourceFile.lifecycle_status)
            )
        ).all()
    assert rows == [("atomic-order-old", SourceFileLifecycle.ACTIVE)]


def _minimal_source(
    sha: str,
    audit_cycle_id: int | None,
    lifecycle: SourceFileLifecycle,
    department: Department = Department.REGIONAL,
) -> SourceFile:
    return SourceFile(
        audit_cycle_id=audit_cycle_id,
        department=department,
        report_date=REPORT_DATE,
        sha256=sha,
        original_filename=f"{sha}.xls",
        fingerprint_name="test",
        status=SourceFileStatus.VALID,
        lifecycle_status=lifecycle,
        reported_grand_totals={"total_debt": "100.00"},
    )
