"""AuditReport enqueue, recovery, build-claim, and CORE artifact completion (Stage 4.2).

Excel generation runs outside the claim lock. Success writes AuditArtifact(CORE)
+ summary_json + READY in one transaction — READY without CORE is forbidden.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.comparison_service import (
    compare_cycles,
    find_previous_completed_cycle,
    summarize_comparison,
)
from app.application.report_workbook import build_core_excel_bytes
from app.domain.models import (
    AuditArtifact,
    AuditArtifactKind,
    AuditCycle,
    AuditCycleStatus,
    AuditReport,
    AuditReportStatus,
    SourceFile,
    SourceFileLifecycle,
)

logger = logging.getLogger(__name__)

GENERATOR_VERSION = "stage4.2.1"
SCHEMA_VERSION = "stage4.v1"


class ReportServiceError(RuntimeError):
    pass


class ReportBuildClaimError(ReportServiceError):
    pass


@dataclass(frozen=True)
class BuildClaim:
    report_id: int
    audit_cycle_id: int
    claim_token: uuid.UUID
    attempt_count: int


@dataclass(frozen=True)
class BuildResult:
    summary_json: dict[str, Any]
    excel_bytes: bytes
    excel_sha256: str


def _ensure_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _claim_is_active(
    *,
    token: uuid.UUID | None,
    claimed_at: dt.datetime | None,
    now_utc: dt.datetime,
    claim_ttl_seconds: int,
) -> bool:
    if token is None or claimed_at is None:
        return False
    age = (now_utc - _ensure_utc(claimed_at)).total_seconds()
    return age < claim_ttl_seconds


def compute_input_hash(
    *,
    cycle_id: int,
    previous_cycle_id: int | None,
    active_files: list[tuple[int, str]],
    generator_version: str = GENERATOR_VERSION,
    schema_version: str = SCHEMA_VERSION,
) -> str:
    parts = [
        f"cycle:{cycle_id}",
        f"previous:{previous_cycle_id if previous_cycle_id is not None else 'none'}",
        f"generator:{generator_version}",
        f"schema:{schema_version}",
    ]
    for file_id, sha256 in sorted(active_files, key=lambda item: item[0]):
        parts.append(f"file:{file_id}:{sha256}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


async def _active_file_fingerprints(
    session: AsyncSession, cycle_id: int
) -> list[tuple[int, str]]:
    rows = (
        await session.execute(
            select(SourceFile.id, SourceFile.sha256).where(
                SourceFile.audit_cycle_id == cycle_id,
                SourceFile.lifecycle_status == SourceFileLifecycle.ACTIVE,
            )
        )
    ).all()
    return [(int(file_id), str(sha256)) for file_id, sha256 in rows]


async def enqueue_pending_report(
    session: AsyncSession,
    cycle: AuditCycle,
) -> AuditReport:
    """Insert AuditReport(PENDING) for a just-COMPLETED cycle (same txn).

    Idempotent: returns existing row if already present.
    """
    existing = await session.scalar(
        select(AuditReport).where(AuditReport.audit_cycle_id == cycle.id)
    )
    if existing is not None:
        return existing

    previous = await find_previous_completed_cycle(
        session, current_report_date=cycle.report_date
    )
    previous_id = previous.id if previous is not None else None
    files = await _active_file_fingerprints(session, cycle.id)
    input_hash = compute_input_hash(
        cycle_id=cycle.id,
        previous_cycle_id=previous_id,
        active_files=files,
    )
    report = AuditReport(
        audit_cycle_id=cycle.id,
        previous_cycle_id=previous_id,
        status=AuditReportStatus.PENDING,
        generator_version=GENERATOR_VERSION,
        schema_version=SCHEMA_VERSION,
        input_hash=input_hash,
    )
    session.add(report)
    await session.flush()
    return report


async def recover_missing_reports(session: AsyncSession) -> list[int]:
    """Defense in depth: COMPLETED cycles without AuditReport → insert PENDING."""
    created: list[int] = []
    async with session.begin():
        cycles = (
            await session.execute(
                select(AuditCycle)
                .where(AuditCycle.status == AuditCycleStatus.COMPLETED)
                .order_by(AuditCycle.id)
            )
        ).scalars().all()
        for cycle in cycles:
            existing = await session.scalar(
                select(AuditReport.id).where(AuditReport.audit_cycle_id == cycle.id)
            )
            if existing is not None:
                continue
            report = await enqueue_pending_report(session, cycle)
            created.append(report.id)
    return created


async def recover_stale_building_reports(
    session: AsyncSession,
    *,
    settings: object,
    now_utc: dt.datetime | None = None,
) -> list[int]:
    """Scheduler-safe TTL recovery: stale BUILDING → FAILED with next_retry_at=now.

    Uses FOR UPDATE SKIP LOCKED so concurrent scheduler ticks do not collide.
    Reports with build_attempt_count >= MAX stay FAILED without next_retry_at
    after the reclaim (terminal until manual intervention).
    """
    now = _ensure_utc(now_utc or dt.datetime.now(dt.timezone.utc))
    claim_ttl = int(settings.report_build_claim_ttl_seconds)  # type: ignore[attr-defined]
    max_attempts = int(settings.report_build_max_attempts)  # type: ignore[attr-defined]
    recovered: list[int] = []

    async with session.begin():
        rows = (
            await session.execute(
                select(AuditReport)
                .where(AuditReport.status == AuditReportStatus.BUILDING)
                .order_by(AuditReport.id)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for report in rows:
            if _claim_is_active(
                token=report.build_claim_token,
                claimed_at=report.build_claimed_at,
                now_utc=now,
                claim_ttl_seconds=claim_ttl,
            ):
                continue
            report.status = AuditReportStatus.FAILED
            report.last_build_error = "build_claim_ttl_expired"
            report.build_claim_token = None
            report.build_claimed_at = None
            if report.build_attempt_count >= max_attempts:
                report.next_retry_at = None
            else:
                report.next_retry_at = now
            recovered.append(report.id)
        await session.flush()
    return recovered


async def list_buildable_report_ids(session: AsyncSession) -> list[int]:
    """List PENDING/FAILED ids only (no TTL side effects). Prefer prepare_*."""
    async with session.begin():
        rows = (
            await session.execute(
                select(AuditReport.id)
                .where(
                    AuditReport.status.in_(
                        (AuditReportStatus.PENDING, AuditReportStatus.FAILED)
                    )
                )
                .order_by(AuditReport.id)
            )
        ).scalars().all()
        return list(rows)


async def prepare_buildable_report_ids(
    session_maker: object,
    *,
    settings: object,
    now_utc: dt.datetime | None = None,
) -> list[int]:
    """Scheduler entrypoint: recover stale BUILDING, then list retryable reports.

    ``session_maker`` must be an ``async_sessionmaker`` (callable returning session).
    Recovery and listing use separate short transactions, matching a scheduler tick.
    """
    now = _ensure_utc(now_utc or dt.datetime.now(dt.timezone.utc))
    max_attempts = int(settings.report_build_max_attempts)  # type: ignore[attr-defined]

    async with session_maker() as session:  # type: ignore[operator]
        await recover_stale_building_reports(
            session, settings=settings, now_utc=now
        )

    async with session_maker() as session:  # type: ignore[operator]
        async with session.begin():
            rows = (
                await session.execute(
                    select(AuditReport)
                    .where(
                        AuditReport.status.in_(
                            (AuditReportStatus.PENDING, AuditReportStatus.FAILED)
                        )
                    )
                    .order_by(AuditReport.id)
                )
            ).scalars().all()
            result: list[int] = []
            for report in rows:
                if report.status == AuditReportStatus.FAILED:
                    if report.build_attempt_count >= max_attempts:
                        continue
                    if report.next_retry_at is not None and _ensure_utc(
                        report.next_retry_at
                    ) > now:
                        continue
                result.append(report.id)
            return result


async def claim_report_build(
    session: AsyncSession,
    *,
    report_id: int,
    settings: object,
    now_utc: dt.datetime | None = None,
) -> BuildClaim | None:
    """Atomic PENDING/FAILED → BUILDING. Commit before generation.

    Does not reclaim stale BUILDING — that is ``recover_stale_building_reports`` /
    ``prepare_buildable_report_ids`` responsibility for the scheduler flow.
    """
    now = _ensure_utc(now_utc or dt.datetime.now(dt.timezone.utc))
    max_attempts = int(settings.report_build_max_attempts)  # type: ignore[attr-defined]

    async with session.begin():
        report = await session.scalar(
            select(AuditReport).where(AuditReport.id == report_id).with_for_update()
        )
        if report is None:
            return None
        if report.status not in (
            AuditReportStatus.PENDING,
            AuditReportStatus.FAILED,
        ):
            return None

        if report.status == AuditReportStatus.FAILED:
            if report.build_attempt_count >= max_attempts:
                return None
            if report.next_retry_at is not None and _ensure_utc(
                report.next_retry_at
            ) > now:
                return None

        if _claim_is_active(
            token=report.build_claim_token,
            claimed_at=report.build_claimed_at,
            now_utc=now,
            claim_ttl_seconds=int(
                settings.report_build_claim_ttl_seconds  # type: ignore[attr-defined]
            ),
        ):
            return None

        token = uuid.uuid4()
        report.status = AuditReportStatus.BUILDING
        report.build_claim_token = token
        report.build_claimed_at = func.clock_timestamp()
        report.build_attempt_count = int(report.build_attempt_count) + 1
        report.next_retry_at = None
        await session.flush()
        return BuildClaim(
            report_id=report.id,
            audit_cycle_id=report.audit_cycle_id,
            claim_token=token,
            attempt_count=report.build_attempt_count,
        )


async def fail_report_build(
    session: AsyncSession,
    *,
    report_id: int,
    claim_token: uuid.UUID,
    error: str,
    settings: object,
    now_utc: dt.datetime | None = None,
) -> bool:
    now = _ensure_utc(now_utc or dt.datetime.now(dt.timezone.utc))
    backoff = int(settings.report_build_backoff_seconds)  # type: ignore[attr-defined]
    max_attempts = int(settings.report_build_max_attempts)  # type: ignore[attr-defined]

    async with session.begin():
        report = await session.scalar(
            select(AuditReport).where(AuditReport.id == report_id).with_for_update()
        )
        if report is None or report.build_claim_token != claim_token:
            return False
        if report.status != AuditReportStatus.BUILDING:
            return False

        report.status = AuditReportStatus.FAILED
        report.last_build_error = error[:2000]
        report.build_claim_token = None
        report.build_claimed_at = None
        if report.build_attempt_count >= max_attempts:
            report.next_retry_at = None
        else:
            delay = backoff * max(1, int(report.build_attempt_count))
            report.next_retry_at = now + dt.timedelta(seconds=delay)
        await session.flush()
        return True


async def complete_report_build(
    session: AsyncSession,
    *,
    report_id: int,
    claim_token: uuid.UUID,
    summary_json: dict[str, Any],
    excel_bytes: bytes,
    excel_sha256: str,
) -> bool:
    """Atomically store CORE artifact + summary and mark READY.

    READY without a CORE artifact is forbidden.
    """
    if not excel_bytes:
        raise ReportServiceError("CORE excel_bytes required for READY")
    expected = hashlib.sha256(excel_bytes).hexdigest()
    if excel_sha256 != expected:
        raise ReportServiceError("excel_sha256 does not match excel_bytes")

    async with session.begin():
        report = await session.scalar(
            select(AuditReport).where(AuditReport.id == report_id).with_for_update()
        )
        if report is None or report.build_claim_token != claim_token:
            return False
        if report.status != AuditReportStatus.BUILDING:
            return False

        existing_core = await session.scalar(
            select(AuditArtifact.id).where(
                AuditArtifact.audit_report_id == report_id,
                AuditArtifact.kind == AuditArtifactKind.CORE,
                AuditArtifact.revision == 1,
            )
        )
        if existing_core is not None:
            raise ReportServiceError("CORE artifact already exists for report")

        session.add(
            AuditArtifact(
                audit_report_id=report.id,
                kind=AuditArtifactKind.CORE,
                revision=1,
                excel_bytes=excel_bytes,
                excel_sha256=excel_sha256,
                financial_input_hash=report.input_hash,
                enrichment_input_hash=None,
                generator_version=report.generator_version,
                schema_version=report.schema_version,
            )
        )
        report.status = AuditReportStatus.READY
        report.summary_json = summary_json
        report.built_at = func.clock_timestamp()
        report.build_claim_token = None
        report.build_claimed_at = None
        report.last_build_error = None
        report.next_retry_at = None
        await session.flush()
        return True


async def run_claimed_build(
    session: AsyncSession,
    *,
    claim: BuildClaim,
) -> BuildResult:
    """Load comparison under a short read txn, then build CORE Excel outside it."""
    async with session.begin():
        report = await session.scalar(
            select(AuditReport).where(AuditReport.id == claim.report_id)
        )
        if report is None or report.build_claim_token != claim.claim_token:
            raise ReportBuildClaimError("claim token mismatch before generate")
        cycle = await session.scalar(
            select(AuditCycle).where(AuditCycle.id == report.audit_cycle_id)
        )
        if cycle is None:
            raise ReportBuildClaimError("audit cycle missing")

        previous = None
        if report.previous_cycle_id is not None:
            previous = await session.scalar(
                select(AuditCycle).where(AuditCycle.id == report.previous_cycle_id)
            )

        comparison = await compare_cycles(
            session, current_cycle=cycle, previous_cycle=previous
        )
        generator_version = report.generator_version
        schema_version = report.schema_version
        input_hash = report.input_hash

    # Excel generation intentionally runs after the DB transaction is closed.
    summary = summarize_comparison(comparison).as_dict()
    summary["generator_version"] = generator_version
    summary["schema_version"] = schema_version
    summary["input_hash"] = input_hash
    excel_bytes, excel_sha256 = build_core_excel_bytes(comparison)
    summary["excel_sha256"] = excel_sha256
    return BuildResult(
        summary_json=summary,
        excel_bytes=excel_bytes,
        excel_sha256=excel_sha256,
    )
