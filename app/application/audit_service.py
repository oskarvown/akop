"""Atomic weekly-audit operations and read-only lookup DTOs."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import NoReturn

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import Department
from app.domain.models import (
    AuditCycle,
    AuditCycleStatus,
    SourceFile,
    SourceFileLifecycle,
)
from app.infrastructure.excel.persistence import persist_valid_source_file
from app.infrastructure.excel.validator import ValidationResult


class AuditServiceError(RuntimeError):
    """Base class for expected Stage 3 business errors."""


class DuplicateSourceFileError(AuditServiceError):
    pass


class CycleImmutableError(AuditServiceError):
    def __init__(self, report_date: dt.date, status: AuditCycleStatus) -> None:
        self.report_date = report_date
        self.status = status
        super().__init__(f"AuditCycle {report_date} is immutable ({status.value})")


class DepartmentSlotTakenError(AuditServiceError):
    pass


class StaleReplacementError(AuditServiceError):
    pass


class AuditCycleNotFoundError(AuditServiceError):
    pass


@dataclass(frozen=True)
class SourceFileLookup:
    id: int
    report_date: dt.date
    department: Department
    lifecycle_status: SourceFileLifecycle
    audit_cycle_id: int | None


@dataclass(frozen=True)
class AuditCycleLookup:
    id: int
    report_date: dt.date
    status: AuditCycleStatus


@dataclass(frozen=True)
class ActiveSourceFileLookup:
    id: int
    original_filename: str | None
    total_debt: Decimal | None


@dataclass(frozen=True)
class CycleStatusSummary:
    present: frozenset[Department]
    missing: frozenset[Department]

    @property
    def is_complete(self) -> bool:
        return not self.missing and self.present == frozenset(Department)


@dataclass(frozen=True)
class AddResult:
    cycle_id: int
    report_date: dt.date
    status: AuditCycleStatus
    summary: CycleStatusSummary
    source_file_id: int
    total_debt: Decimal | None


@dataclass(frozen=True)
class CycleStatusView:
    id: int
    report_date: dt.date
    status: AuditCycleStatus
    completed_at: dt.datetime | None
    summary: CycleStatusSummary
    total_debt: Decimal


async def find_source_file_by_sha256(
    session: AsyncSession, sha256: str
) -> SourceFileLookup | None:
    """Return a projection DTO and finish the read-only transaction."""
    async with session.begin():
        row = (
            await session.execute(
                select(
                    SourceFile.id,
                    SourceFile.report_date,
                    SourceFile.department,
                    SourceFile.lifecycle_status,
                    SourceFile.audit_cycle_id,
                ).where(SourceFile.sha256 == sha256)
            )
        ).one_or_none()
        if row is None:
            return None
        return SourceFileLookup(
            id=row.id,
            report_date=row.report_date,
            department=row.department,
            lifecycle_status=row.lifecycle_status,
            audit_cycle_id=row.audit_cycle_id,
        )


async def find_audit_cycle_by_report_date(
    session: AsyncSession, report_date: dt.date
) -> AuditCycleLookup | None:
    async with session.begin():
        row = (
            await session.execute(
                select(AuditCycle.id, AuditCycle.report_date, AuditCycle.status).where(
                    AuditCycle.report_date == report_date
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return AuditCycleLookup(id=row.id, report_date=row.report_date, status=row.status)


async def count_collecting_cycles(session: AsyncSession) -> list[AuditCycleLookup]:
    async with session.begin():
        rows = (
            await session.execute(
                select(AuditCycle.id, AuditCycle.report_date, AuditCycle.status)
                .where(AuditCycle.status == AuditCycleStatus.COLLECTING)
                .order_by(AuditCycle.report_date)
            )
        ).all()
        return [
            AuditCycleLookup(id=row.id, report_date=row.report_date, status=row.status)
            for row in rows
        ]


async def get_active_source_file(
    session: AsyncSession, audit_cycle_id: int, department: Department
) -> ActiveSourceFileLookup | None:
    async with session.begin():
        row = (
            await session.execute(
                select(
                    SourceFile.id,
                    SourceFile.original_filename,
                    SourceFile.reported_grand_totals,
                ).where(
                    SourceFile.audit_cycle_id == audit_cycle_id,
                    SourceFile.department == department,
                    SourceFile.lifecycle_status == SourceFileLifecycle.ACTIVE,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return ActiveSourceFileLookup(
            id=row.id,
            original_filename=row.original_filename,
            total_debt=_reported_total_debt(row.reported_grand_totals),
        )


async def find_audit_cycle_by_report_date_for_update(
    session: AsyncSession, report_date: dt.date
) -> AuditCycle | None:
    """Internal helper; caller must own the active transaction."""
    return await session.scalar(
        select(AuditCycle)
        .where(AuditCycle.report_date == report_date)
        .with_for_update()
    )


async def get_or_create_audit_cycle(
    session: AsyncSession, report_date: dt.date
) -> AuditCycle:
    """Internal helper called only inside an add transaction."""
    cycle = await session.scalar(
        select(AuditCycle)
        .where(AuditCycle.report_date == report_date)
        .with_for_update()
    )
    if cycle is not None:
        return cycle

    cycle = AuditCycle(
        report_date=report_date,
        status=AuditCycleStatus.COLLECTING,
    )
    session.add(cycle)
    await session.flush()
    return cycle


def assert_cycle_mutable(cycle: AuditCycle) -> None:
    if cycle.status != AuditCycleStatus.COLLECTING:
        raise CycleImmutableError(cycle.report_date, cycle.status)


async def cycle_status_summary(
    session: AsyncSession, audit_cycle_id: int
) -> CycleStatusSummary:
    departments = frozenset(
        (
            await session.scalars(
                select(SourceFile.department).where(
                    SourceFile.audit_cycle_id == audit_cycle_id,
                    SourceFile.lifecycle_status == SourceFileLifecycle.ACTIVE,
                )
            )
        ).all()
    )
    all_departments = frozenset(Department)
    return CycleStatusSummary(
        present=departments,
        missing=all_departments - departments,
    )


async def finalize_if_complete(
    session: AsyncSession, cycle: AuditCycle
) -> tuple[CycleStatusSummary, bool]:
    summary = await cycle_status_summary(session, cycle.id)
    if cycle.status == AuditCycleStatus.COLLECTING and summary.is_complete:
        cycle.status = AuditCycleStatus.COMPLETED
        cycle.completed_at = func.clock_timestamp()
        return summary, True
    return summary, False


async def add_source_file_atomic(
    session: AsyncSession,
    *,
    result: ValidationResult,
    department: Department,
    sha256: str,
    original_filename: str | None,
    report_date: dt.date,
) -> AddResult:
    for attempt in range(2):
        try:
            async with session.begin():
                cycle = await get_or_create_audit_cycle(session, report_date)
                assert_cycle_mutable(cycle)
                await _assert_sha256_available(session, sha256)
                await _assert_department_available(session, cycle.id, department)

                source_file = await persist_valid_source_file(
                    session,
                    result=result,
                    department=department,
                    sha256=sha256,
                    original_filename=original_filename,
                    audit_cycle_id=cycle.id,
                    lifecycle_status=SourceFileLifecycle.ACTIVE,
                )
                cycle.last_activity_at = func.clock_timestamp()
                await session.flush()
                summary, finalized = await finalize_if_complete(session, cycle)
                total_debt = _result_total_debt(result)
                return AddResult(
                    cycle_id=cycle.id,
                    report_date=cycle.report_date,
                    status=(
                        AuditCycleStatus.COMPLETED
                        if finalized
                        else AuditCycleStatus.COLLECTING
                    ),
                    summary=summary,
                    source_file_id=source_file.id,
                    total_debt=total_debt,
                )
        except IntegrityError as exc:
            constraint_name = _extract_constraint_name(exc)
            if constraint_name == "uq_audit_cycle_report_date" and attempt == 0:
                continue
            _raise_translated_integrity_error(exc)
    raise AssertionError("unreachable")


async def replace_source_file_atomic(
    session: AsyncSession,
    *,
    result: ValidationResult,
    department: Department,
    sha256: str,
    original_filename: str | None,
    report_date: dt.date,
    expected_active_source_file_id: int,
) -> AddResult:
    try:
        async with session.begin():
            cycle = await find_audit_cycle_by_report_date_for_update(
                session, report_date
            )
            if cycle is None:
                raise AuditCycleNotFoundError(
                    f"AuditCycle for {report_date} does not exist"
                )
            assert_cycle_mutable(cycle)
            await _assert_sha256_available(session, sha256)

            current_active = await session.scalar(
                select(SourceFile)
                .where(
                    SourceFile.audit_cycle_id == cycle.id,
                    SourceFile.department == department,
                    SourceFile.lifecycle_status == SourceFileLifecycle.ACTIVE,
                )
                .with_for_update()
            )
            if (
                current_active is None
                or current_active.id != expected_active_source_file_id
            ):
                raise StaleReplacementError(
                    "active source file changed after confirmation was shown"
                )

            current_active.lifecycle_status = SourceFileLifecycle.SUPERSEDED
            await session.flush()
            source_file = await persist_valid_source_file(
                session,
                result=result,
                department=department,
                sha256=sha256,
                original_filename=original_filename,
                audit_cycle_id=cycle.id,
                lifecycle_status=SourceFileLifecycle.ACTIVE,
            )
            cycle.last_activity_at = func.clock_timestamp()
            await session.flush()
            summary, finalized = await finalize_if_complete(session, cycle)
            return AddResult(
                cycle_id=cycle.id,
                report_date=cycle.report_date,
                status=(
                    AuditCycleStatus.COMPLETED
                    if finalized
                    else AuditCycleStatus.COLLECTING
                ),
                summary=summary,
                source_file_id=source_file.id,
                total_debt=_result_total_debt(result),
            )
    except IntegrityError as exc:
        _raise_translated_integrity_error(exc)


async def list_cycle_statuses(session: AsyncSession) -> list[CycleStatusView]:
    """Read all collecting and the three newest completed cycles as DTOs."""
    async with session.begin():
        cycle_rows = (
            await session.execute(
                select(
                    AuditCycle.id,
                    AuditCycle.report_date,
                    AuditCycle.status,
                    AuditCycle.completed_at,
                )
                .where(
                    AuditCycle.status.in_(
                        (
                            AuditCycleStatus.COLLECTING,
                            AuditCycleStatus.COMPLETED,
                        )
                    )
                )
                .order_by(AuditCycle.report_date.desc())
            )
        ).all()
        collecting = [row for row in cycle_rows if row.status == AuditCycleStatus.COLLECTING]
        completed = [
            row for row in cycle_rows if row.status == AuditCycleStatus.COMPLETED
        ][:3]
        selected = collecting + completed
        if not selected:
            return []

        cycle_ids = [row.id for row in selected]
        file_rows = (
            await session.execute(
                select(
                    SourceFile.audit_cycle_id,
                    SourceFile.department,
                    SourceFile.reported_grand_totals,
                ).where(
                    SourceFile.audit_cycle_id.in_(cycle_ids),
                    SourceFile.lifecycle_status == SourceFileLifecycle.ACTIVE,
                )
            )
        ).all()

        present_by_cycle: dict[int, set[Department]] = {
            cycle_id: set() for cycle_id in cycle_ids
        }
        debt_by_cycle: dict[int, Decimal] = {
            cycle_id: Decimal("0") for cycle_id in cycle_ids
        }
        for file_row in file_rows:
            if file_row.audit_cycle_id is None:
                continue
            present_by_cycle[file_row.audit_cycle_id].add(file_row.department)
            debt = _reported_total_debt(file_row.reported_grand_totals)
            if debt is not None:
                debt_by_cycle[file_row.audit_cycle_id] += debt

        all_departments = frozenset(Department)
        return [
            CycleStatusView(
                id=row.id,
                report_date=row.report_date,
                status=row.status,
                completed_at=row.completed_at,
                summary=CycleStatusSummary(
                    present=frozenset(present_by_cycle[row.id]),
                    missing=all_departments - frozenset(present_by_cycle[row.id]),
                ),
                total_debt=debt_by_cycle[row.id],
            )
            for row in selected
        ]


async def _assert_sha256_available(session: AsyncSession, sha256: str) -> None:
    existing_id = await session.scalar(
        select(SourceFile.id).where(SourceFile.sha256 == sha256)
    )
    if existing_id is not None:
        raise DuplicateSourceFileError("source file with this sha256 already exists")


async def _assert_department_available(
    session: AsyncSession, audit_cycle_id: int, department: Department
) -> None:
    existing_id = await session.scalar(
        select(SourceFile.id).where(
            SourceFile.audit_cycle_id == audit_cycle_id,
            SourceFile.department == department,
            SourceFile.lifecycle_status == SourceFileLifecycle.ACTIVE,
        )
    )
    if existing_id is not None:
        raise DepartmentSlotTakenError(
            f"department {department.value} already has an active file"
        )


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    candidates = (exc.orig, getattr(exc.orig, "orig", None))
    for candidate in candidates:
        if candidate is None:
            continue
        diag = getattr(candidate, "diag", None)
        name = getattr(diag, "constraint_name", None)
        if name:
            return str(name)
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)

    match = re.search(
        r"(uq_audit_cycle_report_date|uq_source_file_sha256|"
        r"uq_source_file_active_per_department)",
        str(exc.orig),
    )
    return match.group(1) if match else None


def _raise_translated_integrity_error(exc: IntegrityError) -> NoReturn:
    constraint_name = _extract_constraint_name(exc)
    if constraint_name == "uq_source_file_sha256":
        raise DuplicateSourceFileError() from exc
    if constraint_name == "uq_source_file_active_per_department":
        raise DepartmentSlotTakenError() from exc
    raise exc


def _reported_total_debt(payload: object) -> Decimal | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("total_debt")
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _result_total_debt(result: ValidationResult) -> Decimal | None:
    if result.parsed is None:
        return None
    return result.parsed.grand_total.total_debt
