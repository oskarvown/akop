"""Stage 4.4 comment enrichment lifecycle (immutable jobs, CORE-isolated)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.application.enriched_workbook import build_enriched_excel_bytes
from app.application.report_delivery_service import enqueue_automatic_delivery
from app.domain.calculations.comment_parser import (
    COMMENT_PARSER_VERSION,
    CommentParseOutcome,
    parse_comment,
)
from app.domain.calculations.comment_redaction import (
    REDACTION_VERSION,
    redact_comment_text,
    redact_counterparty_label,
)
from app.domain.models import (
    AuditArtifact,
    AuditArtifactKind,
    AuditCycle,
    AuditReport,
    AuditReportStatus,
    CommentAnalysis,
    CommentAnalysisConfidence,
    CommentAnalysisSource,
    CommentAnalysisStatus,
    CommentEnrichmentJob,
    CommentEnrichmentJobStatus,
    Counterparty,
    DebtPosition,
    ManagerGroup,
    SourceFile,
    SourceFileLifecycle,
)
from app.infrastructure.llm.openrouter_client import (
    CommentLlmClient,
    OpenRouterClient,
    OpenRouterSchemaError,
    OpenRouterTransientError,
)

logger = logging.getLogger(__name__)

ENRICHED_GENERATOR_VERSION = "enriched-1"
ENRICHED_SCHEMA_VERSION = "enriched-sheet-1"

# Runtime supports only locked Stage 4.4 version "1" implementations.
SUPPORTED_PARSER_VERSIONS = frozenset({COMMENT_PARSER_VERSION})
SUPPORTED_PROMPT_VERSIONS = frozenset({"1"})
SUPPORTED_SCHEMA_VERSIONS_LLM = frozenset({"1"})
SUPPORTED_REDACTION_VERSIONS = frozenset({REDACTION_VERSION})


class EnrichmentConflictError(RuntimeError):
    """Conflicting analysis payload for an existing immutable row."""


class EnrichmentInvariantError(RuntimeError):
    """Broken enrichment/artifact invariant."""


class EnrichmentVersionCompatibilityError(RuntimeError):
    """Frozen job metadata is not executable by this runtime."""


@dataclass(frozen=True)
class EnrichmentClaim:
    job_id: int
    claim_token: uuid.UUID
    attempt_count: int
    audit_report_id: int


@dataclass(frozen=True)
class CompleteEnrichmentResult:
    job_id: int
    artifact_id: int
    revision: int
    idempotent: bool


def _ensure_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _versions(settings: object) -> dict[str, str]:
    model = getattr(settings, "openrouter_model", None) or getattr(
        settings, "llm_model", None
    ) or ""
    return {
        "parser_version": str(settings.comment_parser_version),  # type: ignore[attr-defined]
        "prompt_version": str(settings.comment_prompt_version),  # type: ignore[attr-defined]
        "schema_version_llm": str(settings.comment_schema_version_llm),  # type: ignore[attr-defined]
        "redaction_version": str(settings.comment_redaction_version),  # type: ignore[attr-defined]
        "model_name": str(model),
    }


def _job_versions(job: CommentEnrichmentJob) -> dict[str, str]:
    """Frozen versions from the job row — never live settings."""
    return {
        "parser_version": str(job.parser_version),
        "prompt_version": str(job.prompt_version),
        "schema_version_llm": str(job.schema_version_llm),
        "redaction_version": str(job.redaction_version),
        "model_name": str(job.model_name),
    }


def assert_runtime_supports_job(job: CommentEnrichmentJob) -> None:
    """Fail closed if frozen metadata cannot be executed by this binary."""
    checks = (
        ("parser_version", job.parser_version, SUPPORTED_PARSER_VERSIONS),
        ("prompt_version", job.prompt_version, SUPPORTED_PROMPT_VERSIONS),
        ("schema_version_llm", job.schema_version_llm, SUPPORTED_SCHEMA_VERSIONS_LLM),
        ("redaction_version", job.redaction_version, SUPPORTED_REDACTION_VERSIONS),
    )
    for name, value, supported in checks:
        if str(value) not in supported:
            raise EnrichmentVersionCompatibilityError(
                f"unsupported_frozen_{name}:{value}"
            )


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def compute_analysis_input_hash(
    *,
    comment_raw: str,
    report_date: dt.date,
    versions: Mapping[str, str],
) -> str:
    payload = {
        "comment_raw": comment_raw,
        "report_date": report_date.isoformat(),
        **dict(versions),
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def compute_enrichment_input_hash(
    *,
    snapshot: list[dict[str, Any]],
    report_date: dt.date,
    financial_input_hash: str,
    versions: Mapping[str, str],
) -> str:
    payload = {
        "snapshot": snapshot,
        "report_date": report_date.isoformat(),
        "financial_input_hash": financial_input_hash,
        **dict(versions),
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _is_fenced(job: CommentEnrichmentJob, claim_token: uuid.UUID) -> bool:
    return (
        job.status is CommentEnrichmentJobStatus.CLAIMED
        and job.claim_token == claim_token
    )


def _is_terminal_failed(job: CommentEnrichmentJob, *, max_attempts: int) -> bool:
    return (
        job.status is CommentEnrichmentJobStatus.FAILED
        and int(job.attempt_count) >= max_attempts
        and job.next_retry_at is None
    )


async def _load_snapshot_rows(
    session: AsyncSession, *, audit_cycle_id: int
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(
                DebtPosition.id.label("debt_position_id"),
                DebtPosition.source_file_id.label("source_file_id"),
                DebtPosition.row_order.label("row_order"),
                DebtPosition.outline_level.label("outline_level"),
                DebtPosition.comment_raw.label("comment_raw"),
                SourceFile.department.label("department"),
                ManagerGroup.raw_name.label("manager_group"),
                Counterparty.raw_name.label("counterparty_label"),
            )
            .join(SourceFile, SourceFile.id == DebtPosition.source_file_id)
            .join(ManagerGroup, ManagerGroup.id == DebtPosition.manager_group_id)
            .join(Counterparty, Counterparty.id == DebtPosition.counterparty_id)
            .where(
                SourceFile.audit_cycle_id == audit_cycle_id,
                SourceFile.lifecycle_status == SourceFileLifecycle.ACTIVE,
                DebtPosition.comment_raw.is_not(None),
            )
            .order_by(
                DebtPosition.source_file_id,
                DebtPosition.row_order,
                DebtPosition.id,
            )
        )
    ).all()
    snapshot: list[dict[str, Any]] = []
    for row in rows:
        comment = (row.comment_raw or "").strip()
        if not comment:
            continue
        department = row.department
        department_value = (
            department.value if hasattr(department, "value") else str(department)
        )
        snapshot.append(
            {
                "debt_position_id": int(row.debt_position_id),
                "source_file_id": int(row.source_file_id),
                "row_order": int(row.row_order),
                "department": department_value,
                "manager_group": str(row.manager_group),
                "counterparty_label": str(row.counterparty_label),
                "outline_level": int(row.outline_level),
                "comment_raw": comment,
            }
        )
    return snapshot


async def enqueue_enrichment_job(
    session: AsyncSession,
    *,
    report_id: int,
    settings: object,
) -> int | None:
    """Create or return enrichment job for a READY report. Never touches CORE txn."""
    versions = _versions(settings)
    async with session.begin():
        report = await session.scalar(
            select(AuditReport)
            .where(AuditReport.id == report_id)
            .with_for_update()
        )
        if report is None or report.status is not AuditReportStatus.READY:
            return None
        cycle = await session.scalar(
            select(AuditCycle).where(AuditCycle.id == report.audit_cycle_id)
        )
        if cycle is None:
            return None
        snapshot = await _load_snapshot_rows(
            session, audit_cycle_id=report.audit_cycle_id
        )
        enrichment_hash = compute_enrichment_input_hash(
            snapshot=snapshot,
            report_date=cycle.report_date,
            financial_input_hash=report.input_hash,
            versions=versions,
        )
        existing = await session.scalar(
            select(CommentEnrichmentJob).where(
                CommentEnrichmentJob.audit_report_id == report_id,
                CommentEnrichmentJob.enrichment_input_hash == enrichment_hash,
            )
        )
        if existing is not None:
            return int(existing.id)

        status = (
            CommentEnrichmentJobStatus.SKIPPED
            if not snapshot
            else CommentEnrichmentJobStatus.PENDING
        )
        job = CommentEnrichmentJob(
            audit_report_id=report_id,
            comment_analysis_batch_id=uuid.uuid4(),
            enrichment_input_hash=enrichment_hash,
            status=status,
            prompt_version=versions["prompt_version"],
            model_name=versions["model_name"] or "none",
            schema_version_llm=versions["schema_version_llm"],
            redaction_version=versions["redaction_version"],
            parser_version=versions["parser_version"],
            input_snapshot_json=snapshot,
            skipped_at=func.clock_timestamp() if not snapshot else None,
        )
        session.add(job)
        await session.flush()
        return int(job.id)


async def recover_missing_enrichment_jobs(
    session: AsyncSession, *, settings: object
) -> list[int]:
    created: list[int] = []
    async with session.begin():
        report_ids = (
            await session.execute(
                select(AuditReport.id)
                .where(AuditReport.status == AuditReportStatus.READY)
                .order_by(AuditReport.id)
            )
        ).scalars().all()
    for report_id in report_ids:
        job_id = await enqueue_enrichment_job(
            session, report_id=int(report_id), settings=settings
        )
        if job_id is not None:
            # Detect newly created vs existing is hard; recover is idempotent.
            created.append(job_id)
    return created


async def recover_stale_claimed_enrichment_jobs(
    session: AsyncSession, *, settings: object, now_utc: dt.datetime | None = None
) -> list[int]:
    now = _ensure_utc(now_utc or dt.datetime.now(dt.timezone.utc))
    ttl = int(settings.comment_enrichment_claim_ttl_seconds)  # type: ignore[attr-defined]
    max_attempts = int(settings.comment_enrichment_max_attempts)  # type: ignore[attr-defined]
    backoff = int(settings.comment_enrichment_backoff_seconds)  # type: ignore[attr-defined]
    recovered: list[int] = []
    async with session.begin():
        jobs = (
            await session.execute(
                select(CommentEnrichmentJob)
                .where(CommentEnrichmentJob.status == CommentEnrichmentJobStatus.CLAIMED)
                .order_by(CommentEnrichmentJob.id)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for job in jobs:
            if job.claimed_at is None:
                continue
            claimed_at = _ensure_utc(job.claimed_at)
            if claimed_at + dt.timedelta(seconds=ttl) > now:
                continue
            job.status = CommentEnrichmentJobStatus.FAILED
            job.last_error = "enrichment_claim_ttl_expired"
            job.claim_token = None
            job.claimed_at = None
            if int(job.attempt_count) >= max_attempts:
                job.next_retry_at = None
                job.last_terminal_error = job.last_error
            else:
                job.next_retry_at = now + dt.timedelta(
                    seconds=backoff * max(1, int(job.attempt_count))
                )
            recovered.append(int(job.id))
        await session.flush()
    return recovered


async def list_due_enrichment_job_ids(
    session: AsyncSession,
    *,
    settings: object,
    now_utc: dt.datetime | None = None,
    limit: int | None = None,
) -> list[int]:
    now = _ensure_utc(now_utc or dt.datetime.now(dt.timezone.utc))
    max_attempts = int(settings.comment_enrichment_max_attempts)  # type: ignore[attr-defined]
    row_limit = (
        int(settings.comment_enrichment_batch_size)  # type: ignore[attr-defined]
        if limit is None
        else limit
    )
    if row_limit <= 0:
        return []
    async with session.begin():
        rows = (
            await session.execute(
                select(CommentEnrichmentJob)
                .where(
                    CommentEnrichmentJob.status.in_(
                        (
                            CommentEnrichmentJobStatus.PENDING,
                            CommentEnrichmentJobStatus.FAILED,
                        )
                    )
                )
                .order_by(CommentEnrichmentJob.id)
                .limit(row_limit)
            )
        ).scalars().all()
        due: list[int] = []
        for job in rows:
            if job.status is CommentEnrichmentJobStatus.PENDING:
                due.append(int(job.id))
                continue
            if int(job.attempt_count) >= max_attempts and job.next_retry_at is None:
                continue
            if job.next_retry_at is not None and _ensure_utc(job.next_retry_at) > now:
                continue
            due.append(int(job.id))
        return due


async def claim_enrichment_job(
    session: AsyncSession,
    *,
    job_id: int,
    settings: object,
    now_utc: dt.datetime | None = None,
) -> EnrichmentClaim | None:
    now = _ensure_utc(now_utc or dt.datetime.now(dt.timezone.utc))
    max_attempts = int(settings.comment_enrichment_max_attempts)  # type: ignore[attr-defined]
    async with session.begin():
        job = await session.scalar(
            select(CommentEnrichmentJob)
            .where(CommentEnrichmentJob.id == job_id)
            .with_for_update()
        )
        if job is None or job.status not in (
            CommentEnrichmentJobStatus.PENDING,
            CommentEnrichmentJobStatus.FAILED,
        ):
            return None
        if job.status is CommentEnrichmentJobStatus.FAILED and (
            int(job.attempt_count) >= max_attempts and job.next_retry_at is None
        ):
            return None
        if (
            job.status is CommentEnrichmentJobStatus.FAILED
            and job.next_retry_at is not None
            and _ensure_utc(job.next_retry_at) > now
        ):
            return None
        token = uuid.uuid4()
        job.status = CommentEnrichmentJobStatus.CLAIMED
        job.claim_token = token
        job.claimed_at = func.clock_timestamp()
        job.attempt_count = int(job.attempt_count) + 1
        job.next_retry_at = None
        await session.flush()
        return EnrichmentClaim(
            job_id=int(job.id),
            claim_token=token,
            attempt_count=int(job.attempt_count),
            audit_report_id=int(job.audit_report_id),
        )


def _analysis_payload_dict(row: CommentAnalysis) -> dict[str, Any]:
    return {
        "analysis_input_hash": row.analysis_input_hash,
        "comment_raw": row.comment_raw,
        "source": row.source.value,
        "analysis_status": row.analysis_status.value,
        "confidence": row.confidence.value,
        "mentioned_date": row.mentioned_date.isoformat() if row.mentioned_date else None,
        "mentioned_amount": str(row.mentioned_amount)
        if row.mentioned_amount is not None
        else None,
        "action": row.action,
        "reason": row.reason,
        "responsible_person": row.responsible_person,
        "summary": row.summary,
        "parse_notes": row.parse_notes,
    }


async def record_comment_analysis(
    session: AsyncSession,
    *,
    job_id: int,
    claim_token: uuid.UUID,
    debt_position_id: int,
    analysis_input_hash: str,
    comment_raw: str,
    source: CommentAnalysisSource,
    analysis_status: CommentAnalysisStatus,
    confidence: CommentAnalysisConfidence,
    mentioned_date: dt.date | None = None,
    mentioned_amount: Decimal | None = None,
    action: str | None = None,
    reason: str | None = None,
    responsible_person: str | None = None,
    summary: str | None = None,
    raw_llm_json: dict[str, Any] | None = None,
    parse_notes: str | None = None,
) -> bool:
    """Insert immutable progress; identical replay OK; conflicting payload errors."""
    async with session.begin():
        job = await session.scalar(
            select(CommentEnrichmentJob)
            .where(CommentEnrichmentJob.id == job_id)
            .with_for_update()
        )
        if job is None or not _is_fenced(job, claim_token):
            return False
        existing = await session.scalar(
            select(CommentAnalysis).where(
                CommentAnalysis.enrichment_job_id == job_id,
                CommentAnalysis.debt_position_id == debt_position_id,
            )
        )
        incoming = {
            "analysis_input_hash": analysis_input_hash,
            "comment_raw": comment_raw,
            "source": source.value,
            "analysis_status": analysis_status.value,
            "confidence": confidence.value,
            "mentioned_date": mentioned_date.isoformat() if mentioned_date else None,
            "mentioned_amount": str(mentioned_amount)
            if mentioned_amount is not None
            else None,
            "action": action,
            "reason": reason,
            "responsible_person": responsible_person,
            "summary": summary,
            "parse_notes": parse_notes,
        }
        if existing is not None:
            if _analysis_payload_dict(existing) == incoming:
                return True
            raise EnrichmentConflictError(
                f"comment_analysis_conflict job={job_id} position={debt_position_id}"
            )
        session.add(
            CommentAnalysis(
                enrichment_job_id=job_id,
                debt_position_id=debt_position_id,
                analysis_input_hash=analysis_input_hash,
                comment_raw=comment_raw,
                source=source,
                analysis_status=analysis_status,
                confidence=confidence,
                mentioned_date=mentioned_date,
                mentioned_amount=mentioned_amount,
                action=action,
                reason=reason,
                responsible_person=responsible_person,
                summary=summary,
                raw_llm_json=raw_llm_json,
                parse_notes=parse_notes,
            )
        )
        await session.flush()
        return True


async def fail_enrichment(
    session: AsyncSession,
    *,
    job_id: int,
    claim_token: uuid.UUID,
    error: str,
    settings: object,
    now_utc: dt.datetime | None = None,
) -> bool:
    now = _ensure_utc(now_utc or dt.datetime.now(dt.timezone.utc))
    backoff = int(settings.comment_enrichment_backoff_seconds)  # type: ignore[attr-defined]
    max_attempts = int(settings.comment_enrichment_max_attempts)  # type: ignore[attr-defined]
    async with session.begin():
        job = await session.scalar(
            select(CommentEnrichmentJob)
            .where(CommentEnrichmentJob.id == job_id)
            .with_for_update()
        )
        if job is None or not _is_fenced(job, claim_token):
            return False
        job.status = CommentEnrichmentJobStatus.FAILED
        job.last_error = error[:2000]
        job.claim_token = None
        job.claimed_at = None
        if int(job.attempt_count) >= max_attempts:
            job.next_retry_at = None
            job.last_terminal_error = job.last_error
        else:
            job.next_retry_at = now + dt.timedelta(
                seconds=backoff * max(1, int(job.attempt_count))
            )
        await session.flush()
        return True


async def retry_terminal_enrichment(session: AsyncSession, *, job_id: int) -> bool:
    """Operator reopen: fresh attempt budget, same job/batch/analyses."""
    async with session.begin():
        job = await session.scalar(
            select(CommentEnrichmentJob)
            .where(CommentEnrichmentJob.id == job_id)
            .with_for_update()
        )
        if job is None or job.status is not CommentEnrichmentJobStatus.FAILED:
            return False
        if job.next_retry_at is not None:
            return False
        artifact = await session.scalar(
            select(AuditArtifact.id).where(AuditArtifact.enrichment_job_id == job_id)
        )
        if artifact is not None:
            return False
        job.last_terminal_error = job.last_error
        job.status = CommentEnrichmentJobStatus.PENDING
        job.attempt_count = 0
        job.next_retry_at = None
        job.claim_token = None
        job.claimed_at = None
        job.operator_retry_count = int(job.operator_retry_count) + 1
        await session.flush()
        return True


async def complete_enrichment(
    session: AsyncSession,
    *,
    job_id: int,
    claim_token: uuid.UUID,
    excel_bytes: bytes,
    excel_sha256: str,
    enrichment_counts: dict[str, Any],
) -> CompleteEnrichmentResult:
    """Atomic finalize with idempotent branch before CLAIMED fence."""
    async with session.begin():
        job = await session.scalar(
            select(CommentEnrichmentJob)
            .where(CommentEnrichmentJob.id == job_id)
            .with_for_update()
        )
        if job is None:
            raise EnrichmentInvariantError("job_missing")
        artifact = await session.scalar(
            select(AuditArtifact).where(AuditArtifact.enrichment_job_id == job_id)
        )
        if artifact is not None:
            if job.status is not CommentEnrichmentJobStatus.READY:
                raise EnrichmentInvariantError(
                    "artifact_exists_but_job_not_ready"
                )
            if artifact.enrichment_job_id != job_id:
                raise EnrichmentInvariantError("artifact_job_mismatch")
            return CompleteEnrichmentResult(
                job_id=job_id,
                artifact_id=int(artifact.id),
                revision=int(artifact.revision),
                idempotent=True,
            )
        if not _is_fenced(job, claim_token):
            raise EnrichmentInvariantError("claim_fence_failed")

        report = await session.scalar(
            select(AuditReport)
            .where(AuditReport.id == job.audit_report_id)
            .with_for_update()
        )
        if report is None:
            raise EnrichmentInvariantError("report_missing")

        artifact_again = await session.scalar(
            select(AuditArtifact).where(AuditArtifact.enrichment_job_id == job_id)
        )
        if artifact_again is not None:
            return CompleteEnrichmentResult(
                job_id=job_id,
                artifact_id=int(artifact_again.id),
                revision=int(artifact_again.revision),
                idempotent=True,
            )

        max_revision = await session.scalar(
            select(func.max(AuditArtifact.revision)).where(
                AuditArtifact.audit_report_id == report.id,
                AuditArtifact.kind == AuditArtifactKind.ENRICHED,
            )
        )
        revision = int(max_revision or 0) + 1
        new_artifact = AuditArtifact(
            audit_report_id=report.id,
            kind=AuditArtifactKind.ENRICHED,
            revision=revision,
            excel_bytes=excel_bytes,
            excel_sha256=excel_sha256,
            financial_input_hash=report.input_hash,
            enrichment_input_hash=job.enrichment_input_hash,
            generator_version=ENRICHED_GENERATOR_VERSION,
            schema_version=ENRICHED_SCHEMA_VERSION,
            enrichment_job_id=job.id,
            model_name=job.model_name,
            prompt_version=job.prompt_version,
            schema_version_llm=job.schema_version_llm,
            redaction_version=job.redaction_version,
        )
        session.add(new_artifact)
        await session.flush()

        job.enrichment_counts_json = enrichment_counts
        job.status = CommentEnrichmentJobStatus.READY
        job.ready_at = func.clock_timestamp()
        job.claim_token = None
        job.claimed_at = None
        await session.flush()

        if revision == 1:
            await enqueue_automatic_delivery(session, new_artifact)

        return CompleteEnrichmentResult(
            job_id=job_id,
            artifact_id=int(new_artifact.id),
            revision=revision,
            idempotent=False,
        )


def _make_llm_client(
    settings: object, *, model_name: str
) -> CommentLlmClient | None:
    api_key = getattr(settings, "openrouter_api_key", None) or getattr(
        settings, "llm_api_key", None
    )
    model = (model_name or "").strip()
    if not api_key or not model or model == "none":
        return None
    return OpenRouterClient(
        api_key=str(api_key),
        base_url=str(
            getattr(settings, "openrouter_base_url", "https://openrouter.ai/api/v1")
        ),
        model=str(model),
        timeout_seconds=float(settings.openrouter_timeout_seconds),  # type: ignore[attr-defined]
        max_retries=int(settings.openrouter_max_retries),  # type: ignore[attr-defined]
    )


async def run_claimed_enrichment(
    session_maker: Any,
    *,
    claim: EnrichmentClaim,
    settings: object,
    llm_client: CommentLlmClient | None = None,
    now_utc: dt.datetime | None = None,
) -> CompleteEnrichmentResult | None:
    """Process snapshot comments under run timeout; build XLSX outside DB txn."""
    run_timeout = float(settings.comment_enrichment_run_timeout_seconds)  # type: ignore[attr-defined]

    async with session_maker() as session:
        job = await session.get(CommentEnrichmentJob, claim.job_id)
        if job is None:
            return None
        try:
            assert_runtime_supports_job(job)
        except EnrichmentVersionCompatibilityError as exc:
            async with session_maker() as s2:
                await fail_enrichment(
                    s2,
                    job_id=claim.job_id,
                    claim_token=claim.claim_token,
                    error=str(exc),
                    settings=settings,
                    now_utc=now_utc,
                )
            return None
        versions = _job_versions(job)
        client = (
            llm_client
            if llm_client is not None
            else _make_llm_client(settings, model_name=versions["model_name"])
        )
        report = await session.get(AuditReport, job.audit_report_id)
        cycle = (
            await session.get(AuditCycle, report.audit_cycle_id)
            if report is not None
            else None
        )
        if report is None or cycle is None:
            async with session_maker() as s2:
                await fail_enrichment(
                    s2,
                    job_id=claim.job_id,
                    claim_token=claim.claim_token,
                    error="report_or_cycle_missing",
                    settings=settings,
                    now_utc=now_utc,
                )
            return None
        snapshot = list(job.input_snapshot_json or [])
        existing = (
            await session.execute(
                select(CommentAnalysis).where(
                    CommentAnalysis.enrichment_job_id == claim.job_id
                )
            )
        ).scalars().all()
        done_ids = {
            int(row.debt_position_id)
            for row in existing
            if row.analysis_status
            in (CommentAnalysisStatus.RESOLVED, CommentAnalysisStatus.NEEDS_REVIEW)
        }
        report_date = cycle.report_date
        financial_hash = report.input_hash

    import asyncio

    async def _process() -> CompleteEnrichmentResult:
        for snap in snapshot:
            position_id = int(snap["debt_position_id"])
            if position_id in done_ids:
                continue
            comment_raw = str(snap["comment_raw"])
            analysis_hash = compute_analysis_input_hash(
                comment_raw=comment_raw,
                report_date=report_date,
                versions=versions,
            )
            parsed = parse_comment(
                comment_raw,
                report_date=report_date,
                parser_version=versions["parser_version"],
            )
            if parsed.outcome is CommentParseOutcome.RESOLVED:
                async with session_maker() as session:
                    await record_comment_analysis(
                        session,
                        job_id=claim.job_id,
                        claim_token=claim.claim_token,
                        debt_position_id=position_id,
                        analysis_input_hash=analysis_hash,
                        comment_raw=comment_raw,
                        source=CommentAnalysisSource.DETERMINISTIC,
                        analysis_status=CommentAnalysisStatus.RESOLVED,
                        confidence=CommentAnalysisConfidence(parsed.confidence),
                        mentioned_date=parsed.mentioned_date,
                        mentioned_amount=parsed.mentioned_amount,
                        action=parsed.action,
                        reason=parsed.reason,
                        responsible_person=parsed.responsible_person,
                        summary=parsed.summary,
                        parse_notes=parsed.parse_notes,
                    )
                done_ids.add(position_id)
                continue

            # Ambiguous
            if client is None:
                async with session_maker() as session:
                    await record_comment_analysis(
                        session,
                        job_id=claim.job_id,
                        claim_token=claim.claim_token,
                        debt_position_id=position_id,
                        analysis_input_hash=analysis_hash,
                        comment_raw=comment_raw,
                        source=CommentAnalysisSource.UNPARSED,
                        analysis_status=CommentAnalysisStatus.NEEDS_REVIEW,
                        confidence=CommentAnalysisConfidence.NONE,
                        summary=parsed.summary or comment_raw[:200],
                        parse_notes="missing_api_key",
                    )
                done_ids.add(position_id)
                continue

            redacted_comment = redact_comment_text(
                comment_raw, redaction_version=versions["redaction_version"]
            )
            redacted_label = redact_counterparty_label(
                str(snap.get("counterparty_label") or "") or None,
                redaction_version=versions["redaction_version"],
            )
            try:
                facts = await client.analyze_comment(
                    comment_raw=redacted_comment,
                    report_date=report_date,
                    counterparty_label=redacted_label,
                )
            except OpenRouterTransientError:
                raise
            except OpenRouterSchemaError as exc:
                async with session_maker() as session:
                    await record_comment_analysis(
                        session,
                        job_id=claim.job_id,
                        claim_token=claim.claim_token,
                        debt_position_id=position_id,
                        analysis_input_hash=analysis_hash,
                        comment_raw=comment_raw,
                        source=CommentAnalysisSource.UNPARSED,
                        analysis_status=CommentAnalysisStatus.NEEDS_REVIEW,
                        confidence=CommentAnalysisConfidence.NONE,
                        summary=(parsed.summary or comment_raw[:200]),
                        parse_notes=f"schema_invalid:{type(exc).__name__}",
                    )
                done_ids.add(position_id)
                continue

            async with session_maker() as session:
                await record_comment_analysis(
                    session,
                    job_id=claim.job_id,
                    claim_token=claim.claim_token,
                    debt_position_id=position_id,
                    analysis_input_hash=analysis_hash,
                    comment_raw=comment_raw,
                    source=CommentAnalysisSource.LLM,
                    analysis_status=CommentAnalysisStatus.RESOLVED,
                    confidence=CommentAnalysisConfidence(facts.confidence),
                    mentioned_date=facts.mentioned_date,
                    mentioned_amount=facts.mentioned_amount,
                    action=facts.action,
                    reason=facts.reason,
                    responsible_person=facts.responsible_person,
                    summary=facts.summary,
                    raw_llm_json=facts.raw_json,
                )
            done_ids.add(position_id)

        # Build workbook outside DB txn
        async with session_maker() as session:
            job = await session.get(CommentEnrichmentJob, claim.job_id)
            assert job is not None
            core = await session.scalar(
                select(AuditArtifact).where(
                    AuditArtifact.audit_report_id == job.audit_report_id,
                    AuditArtifact.kind == AuditArtifactKind.CORE,
                    AuditArtifact.revision == 1,
                )
            )
            if core is None:
                raise EnrichmentInvariantError("core_artifact_missing")
            analyses = (
                await session.execute(
                    select(CommentAnalysis).where(
                        CommentAnalysis.enrichment_job_id == claim.job_id
                    )
                )
            ).scalars().all()
            analyses_map = {
                int(row.debt_position_id): {
                    "source": row.source.value,
                    "analysis_status": row.analysis_status.value,
                    "confidence": row.confidence.value,
                    "mentioned_date": row.mentioned_date.isoformat()
                    if row.mentioned_date
                    else None,
                    "mentioned_amount": float(row.mentioned_amount)
                    if row.mentioned_amount is not None
                    else None,
                    "action": row.action,
                    "reason": row.reason,
                    "responsible_person": row.responsible_person,
                    "summary": row.summary,
                }
                for row in analyses
            }
            snapshot_rows = list(job.input_snapshot_json or [])
            core_bytes = bytes(core.excel_bytes)
            counts = {
                "total": len(snapshot_rows),
                "resolved": sum(
                    1
                    for a in analyses
                    if a.analysis_status is CommentAnalysisStatus.RESOLVED
                ),
                "needs_review": sum(
                    1
                    for a in analyses
                    if a.analysis_status is CommentAnalysisStatus.NEEDS_REVIEW
                ),
                "llm": sum(
                    1 for a in analyses if a.source is CommentAnalysisSource.LLM
                ),
                "deterministic": sum(
                    1
                    for a in analyses
                    if a.source is CommentAnalysisSource.DETERMINISTIC
                ),
                "financial_input_hash": financial_hash,
            }

        excel_bytes, excel_sha256 = build_enriched_excel_bytes(
            core_bytes,
            snapshot_rows=snapshot_rows,
            analyses_by_position=analyses_map,
        )
        async with session_maker() as session:
            return await complete_enrichment(
                session,
                job_id=claim.job_id,
                claim_token=claim.claim_token,
                excel_bytes=excel_bytes,
                excel_sha256=excel_sha256,
                enrichment_counts=counts,
            )

    try:
        async with asyncio.timeout(run_timeout):
            return await _process()
    except OpenRouterTransientError as exc:
        async with session_maker() as session:
            await fail_enrichment(
                session,
                job_id=claim.job_id,
                claim_token=claim.claim_token,
                error=str(exc),
                settings=settings,
                now_utc=now_utc,
            )
        return None
    except TimeoutError:
        async with session_maker() as session:
            await fail_enrichment(
                session,
                job_id=claim.job_id,
                claim_token=claim.claim_token,
                error="enrichment_run_timeout",
                settings=settings,
                now_utc=now_utc,
            )
        return None
    except Exception as exc:
        logger.exception("Enrichment run failed job_id=%s", claim.job_id)
        async with session_maker() as session:
            await fail_enrichment(
                session,
                job_id=claim.job_id,
                claim_token=claim.claim_token,
                error=type(exc).__name__,
                settings=settings,
                now_utc=now_utc,
            )
        return None
