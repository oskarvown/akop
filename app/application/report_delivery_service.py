"""Stage 4.3 Telegram report delivery lifecycle.

Delivery is deliberately at-least-once.  Telegram does not offer an idempotency
key, so a process crash after Telegram accepts a message but before its message
id is persisted can result in a repeated message after the claim lease expires.
Persisted summary and document progress prevents re-sending steps which were
successfully recorded before a retry.

Automatic delivery has one lifecycle per ``(audit_artifact_id, channel)``;
including a FAILED lifecycle.  Manual delivery intentionally creates a new
lifecycle on every request.
"""
from __future__ import annotations

import datetime as dt
import enum
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.domain.models import (
    AuditArtifact,
    AuditArtifactKind,
    AuditCycle,
    AuditCycleStatus,
    AuditReport,
    AuditReportStatus,
    ReportDelivery,
    ReportDeliveryChannel,
    ReportDeliveryKind,
    ReportDeliveryStatus,
)

TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024


class ResolveStatus(str, enum.Enum):
    CYCLE_NOT_FOUND = "cycle_not_found"
    CYCLE_COLLECTING = "cycle_collecting"
    CYCLE_EXPIRED = "cycle_expired"
    REPORT_MISSING = "report_missing"
    REPORT_PENDING = "report_pending"
    REPORT_BUILDING = "report_building"
    REPORT_FAILED = "report_failed"
    ARTIFACT_MISSING = "artifact_missing"
    READY = "ready"


@dataclass(frozen=True)
class ResolveResult:
    """Result of resolving an artifact suitable for a Telegram command handler."""

    status: ResolveStatus
    cycle_id: int | None = None
    report_id: int | None = None
    artifact_id: int | None = None
    artifact_kind: AuditArtifactKind | None = None
    artifact_revision: int | None = None
    report_date: dt.date | None = None
    input_hash: str | None = None
    notification_chat_id: int | None = None

    @property
    def ready(self) -> bool:
        return self.status is ResolveStatus.READY

    @property
    def user_message(self) -> str:
        """A safe, concise Russian explanation for a command handler."""
        messages = {
            ResolveStatus.CYCLE_NOT_FOUND: "Аудит за указанную дату не найден.",
            ResolveStatus.CYCLE_COLLECTING: "Аудит ещё собирается; отчёт пока недоступен.",
            ResolveStatus.CYCLE_EXPIRED: "Сбор аудита истёк; отчёт не был сформирован.",
            ResolveStatus.REPORT_MISSING: "Отчёт для завершённого аудита ещё не поставлен в очередь.",
            ResolveStatus.REPORT_PENDING: "Отчёт поставлен в очередь и скоро будет сформирован.",
            ResolveStatus.REPORT_BUILDING: "Отчёт сейчас формируется.",
            ResolveStatus.REPORT_FAILED: "Отчёт пока не удалось сформировать; будет выполнена повторная попытка.",
            ResolveStatus.ARTIFACT_MISSING: "Файл отчёта пока недоступен.",
            ResolveStatus.READY: "Отчёт готов.",
        }
        return messages[self.status]


@dataclass(frozen=True)
class DeliveryClaim:
    delivery_id: int
    claim_token: uuid.UUID
    destination_chat_id: int
    audit_artifact_id: int
    attempt_count: int


@dataclass(frozen=True)
class DeliverySendContext:
    """Immutable data needed by the worker outside its short database transaction."""

    delivery_id: int
    document_message_id: int | None
    summary_sent_count: int
    excel_bytes: bytes
    excel_sha256: str
    artifact_kind: AuditArtifactKind
    artifact_revision: int
    report_date: dt.date
    summary_json: dict[str, Any] | None
    summary_messages: list[str]
    caption: str
    filename: str

    @property
    def document_already_sent(self) -> bool:
        return self.document_message_id is not None


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
    return (now_utc - _ensure_utc(claimed_at)).total_seconds() < claim_ttl_seconds


def _is_fenced(delivery: ReportDelivery, claim_token: uuid.UUID) -> bool:
    return (
        delivery.status == ReportDeliveryStatus.CLAIMED
        and delivery.claim_token == claim_token
    )


async def enqueue_automatic_delivery(
    session: AsyncSession, artifact: AuditArtifact
) -> ReportDelivery | None:
    """Create the sole automatic Telegram lifecycle for a CORE artifact.

    This small helper intentionally joins the caller's transaction: CORE artifact
    creation and its automatic outbox row can therefore be committed together.
    """
    if artifact.kind != AuditArtifactKind.CORE:
        return None

    existing = await session.scalar(
        select(ReportDelivery).where(
            ReportDelivery.audit_artifact_id == artifact.id,
            ReportDelivery.channel == ReportDeliveryChannel.TELEGRAM,
            ReportDelivery.kind == ReportDeliveryKind.AUTOMATIC,
        )
    )
    if existing is not None:
        return existing

    report = await session.scalar(
        select(AuditReport).where(AuditReport.id == artifact.audit_report_id)
    )
    if report is None:
        return None
    cycle = await session.scalar(
        select(AuditCycle).where(AuditCycle.id == report.audit_cycle_id)
    )
    if cycle is None:
        return None

    delivery = ReportDelivery(
        audit_artifact_id=artifact.id,
        channel=ReportDeliveryChannel.TELEGRAM,
        kind=ReportDeliveryKind.AUTOMATIC,
        status=ReportDeliveryStatus.PENDING,
        destination_chat_id=cycle.notification_chat_id,
        requested_by_user_id=None,
    )
    session.add(delivery)
    await session.flush()
    return delivery


async def recover_missing_automatic_deliveries(session: AsyncSession) -> list[int]:
    """Backfill automatic rows for all persisted CORE artifacts."""
    created: list[int] = []
    async with session.begin():
        artifacts = (
            await session.execute(
                select(AuditArtifact)
                .where(AuditArtifact.kind == AuditArtifactKind.CORE)
                .order_by(AuditArtifact.id)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for artifact in artifacts:
            existing = await session.scalar(
                select(ReportDelivery.id).where(
                    ReportDelivery.audit_artifact_id == artifact.id,
                    ReportDelivery.channel == ReportDeliveryChannel.TELEGRAM,
                    ReportDelivery.kind == ReportDeliveryKind.AUTOMATIC,
                )
            )
            if existing is None:
                delivery = await enqueue_automatic_delivery(session, artifact)
                if delivery is not None:
                    created.append(delivery.id)
    return created


async def recover_stale_claimed_deliveries(
    session: AsyncSession, *, settings: object, now_utc: dt.datetime | None = None
) -> list[int]:
    """Release expired claims while retaining partial Telegram send progress."""
    now = _ensure_utc(now_utc or dt.datetime.now(dt.timezone.utc))
    ttl = int(settings.report_delivery_claim_ttl_seconds)  # type: ignore[attr-defined]
    max_attempts = int(settings.report_delivery_max_attempts)  # type: ignore[attr-defined]
    recovered: list[int] = []
    async with session.begin():
        rows = (
            await session.execute(
                select(ReportDelivery)
                .where(ReportDelivery.status == ReportDeliveryStatus.CLAIMED)
                .order_by(ReportDelivery.id)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for delivery in rows:
            if _claim_is_active(
                token=delivery.claim_token,
                claimed_at=delivery.claimed_at,
                now_utc=now,
                claim_ttl_seconds=ttl,
            ):
                continue
            delivery.status = ReportDeliveryStatus.FAILED
            delivery.last_error = "delivery_claim_ttl_expired"
            delivery.claim_token = None
            delivery.claimed_at = None
            delivery.next_retry_at = (
                None if delivery.attempt_count >= max_attempts else now
            )
            recovered.append(delivery.id)
        await session.flush()
    return recovered


async def list_due_delivery_ids(
    session: AsyncSession,
    *,
    settings: object,
    now_utc: dt.datetime | None = None,
    limit: int | None = None,
) -> list[int]:
    """Return PENDING and retryable FAILED delivery ids without claiming them."""
    now = _ensure_utc(now_utc or dt.datetime.now(dt.timezone.utc))
    max_attempts = int(settings.report_delivery_max_attempts)  # type: ignore[attr-defined]
    row_limit = int(settings.report_delivery_batch_size) if limit is None else limit
    if row_limit <= 0:
        return []
    async with session.begin():
        rows = (
            await session.execute(
                select(ReportDelivery.id)
                .where(
                    or_(
                        ReportDelivery.status == ReportDeliveryStatus.PENDING,
                        and_(
                            ReportDelivery.status == ReportDeliveryStatus.FAILED,
                            ReportDelivery.attempt_count < max_attempts,
                            or_(
                                ReportDelivery.next_retry_at.is_(None),
                                ReportDelivery.next_retry_at <= now,
                            ),
                        ),
                    )
                )
                .order_by(ReportDelivery.id)
                .limit(row_limit)
            )
        ).scalars().all()
        return list(rows)


async def claim_delivery(
    session: AsyncSession,
    *,
    delivery_id: int,
    settings: object,
    now_utc: dt.datetime | None = None,
) -> DeliveryClaim | None:
    """Atomically claim a due lifecycle; Telegram I/O happens after commit."""
    now = _ensure_utc(now_utc or dt.datetime.now(dt.timezone.utc))
    max_attempts = int(settings.report_delivery_max_attempts)  # type: ignore[attr-defined]
    async with session.begin():
        delivery = await session.scalar(
            select(ReportDelivery)
            .where(ReportDelivery.id == delivery_id)
            .with_for_update()
        )
        if delivery is None or delivery.status not in (
            ReportDeliveryStatus.PENDING,
            ReportDeliveryStatus.FAILED,
        ):
            return None
        if delivery.status == ReportDeliveryStatus.FAILED and (
            delivery.attempt_count >= max_attempts
            or (
                delivery.next_retry_at is not None
                and _ensure_utc(delivery.next_retry_at) > now
            )
        ):
            return None
        token = uuid.uuid4()
        delivery.status = ReportDeliveryStatus.CLAIMED
        delivery.claim_token = token
        delivery.claimed_at = func.clock_timestamp()
        delivery.attempt_count = int(delivery.attempt_count) + 1
        delivery.next_retry_at = None
        await session.flush()
        return DeliveryClaim(
            delivery_id=delivery.id,
            claim_token=token,
            destination_chat_id=delivery.destination_chat_id,
            audit_artifact_id=delivery.audit_artifact_id,
            attempt_count=delivery.attempt_count,
        )


async def complete_delivery(
    session: AsyncSession, *, delivery_id: int, claim_token: uuid.UUID
) -> bool:
    """Fence and complete a lifecycle only after its document message is recorded."""
    async with session.begin():
        delivery = await session.scalar(
            select(ReportDelivery)
            .where(ReportDelivery.id == delivery_id)
            .with_for_update()
        )
        if delivery is None or not _is_fenced(delivery, claim_token):
            return False
        if delivery.document_message_id is None:
            return False
        delivery.status = ReportDeliveryStatus.DELIVERED
        delivery.delivered_at = func.clock_timestamp()
        delivery.claim_token = None
        delivery.claimed_at = None
        delivery.next_retry_at = None
        await session.flush()
        return True


async def fail_delivery(
    session: AsyncSession,
    *,
    delivery_id: int,
    claim_token: uuid.UUID,
    error: str,
    settings: object,
    now_utc: dt.datetime | None = None,
) -> bool:
    """Fence, release, and schedule a retry after a Telegram failure."""
    now = _ensure_utc(now_utc or dt.datetime.now(dt.timezone.utc))
    backoff = int(settings.report_delivery_backoff_seconds)  # type: ignore[attr-defined]
    max_attempts = int(settings.report_delivery_max_attempts)  # type: ignore[attr-defined]
    async with session.begin():
        delivery = await session.scalar(
            select(ReportDelivery)
            .where(ReportDelivery.id == delivery_id)
            .with_for_update()
        )
        if delivery is None or not _is_fenced(delivery, claim_token):
            return False
        delivery.status = ReportDeliveryStatus.FAILED
        delivery.last_error = error[:2000]
        delivery.claim_token = None
        delivery.claimed_at = None
        delivery.next_retry_at = (
            None
            if delivery.attempt_count >= max_attempts
            else now
            + dt.timedelta(seconds=backoff * max(1, int(delivery.attempt_count)))
        )
        await session.flush()
        return True


async def record_summary_message_sent(
    session: AsyncSession,
    *,
    delivery_id: int,
    claim_token: uuid.UUID,
    message_id: int,
) -> bool:
    """Fence and append a successfully persisted summary-message id."""
    async with session.begin():
        delivery = await session.scalar(
            select(ReportDelivery)
            .where(ReportDelivery.id == delivery_id)
            .with_for_update()
        )
        if delivery is None or not _is_fenced(delivery, claim_token):
            return False
        message_ids = list(delivery.summary_message_ids or [])
        message_ids.append(int(message_id))
        delivery.summary_message_ids = message_ids
        delivery.summary_sent_count = int(delivery.summary_sent_count) + 1
        await session.flush()
        return True


async def record_document_sent(
    session: AsyncSession,
    *,
    delivery_id: int,
    claim_token: uuid.UUID,
    message_id: int,
) -> bool:
    """Fence and persist the document message id before lifecycle completion."""
    async with session.begin():
        delivery = await session.scalar(
            select(ReportDelivery)
            .where(ReportDelivery.id == delivery_id)
            .with_for_update()
        )
        if delivery is None or not _is_fenced(delivery, claim_token):
            return False
        if delivery.document_message_id is not None:
            return delivery.document_message_id == int(message_id)
        delivery.document_message_id = int(message_id)
        await session.flush()
        return True


async def resolve_report_artifact(
    session: AsyncSession, *, report_date: dt.date | None = None, force_core: bool = False
) -> ResolveResult:
    """Resolve the selected (or latest COMPLETED) audit into a ready report artifact."""
    async with session.begin():
        if report_date is not None:
            cycle = await session.scalar(
                select(AuditCycle).where(AuditCycle.report_date == report_date)
            )
        else:
            cycle = await session.scalar(
                select(AuditCycle)
                .where(AuditCycle.status == AuditCycleStatus.COMPLETED)
                .order_by(AuditCycle.report_date.desc())
                .limit(1)
            )
        if cycle is None:
            return ResolveResult(ResolveStatus.CYCLE_NOT_FOUND)
        if cycle.status == AuditCycleStatus.COLLECTING:
            return ResolveResult(
                ResolveStatus.CYCLE_COLLECTING,
                cycle_id=cycle.id,
                report_date=cycle.report_date,
                notification_chat_id=cycle.notification_chat_id,
            )
        if cycle.status == AuditCycleStatus.EXPIRED:
            return ResolveResult(
                ResolveStatus.CYCLE_EXPIRED,
                cycle_id=cycle.id,
                report_date=cycle.report_date,
                notification_chat_id=cycle.notification_chat_id,
            )
        report = await session.scalar(
            select(AuditReport).where(AuditReport.audit_cycle_id == cycle.id)
        )
        if report is None:
            return ResolveResult(
                ResolveStatus.REPORT_MISSING,
                cycle_id=cycle.id,
                report_date=cycle.report_date,
                notification_chat_id=cycle.notification_chat_id,
            )
        statuses = {
            AuditReportStatus.PENDING: ResolveStatus.REPORT_PENDING,
            AuditReportStatus.BUILDING: ResolveStatus.REPORT_BUILDING,
            AuditReportStatus.FAILED: ResolveStatus.REPORT_FAILED,
        }
        if report.status != AuditReportStatus.READY:
            return ResolveResult(
                statuses[report.status],
                cycle_id=cycle.id,
                report_id=report.id,
                report_date=cycle.report_date,
                notification_chat_id=cycle.notification_chat_id,
            )
        if force_core:
            artifact = await session.scalar(
                select(AuditArtifact).where(
                    AuditArtifact.audit_report_id == report.id,
                    AuditArtifact.kind == AuditArtifactKind.CORE,
                    AuditArtifact.revision == 1,
                )
            )
        else:
            enriched = await session.scalar(
                select(AuditArtifact)
                .where(
                    AuditArtifact.audit_report_id == report.id,
                    AuditArtifact.kind == AuditArtifactKind.ENRICHED,
                )
                .order_by(AuditArtifact.revision.desc())
                .limit(1)
            )
            if enriched is not None:
                artifact = enriched
            else:
                artifact = await session.scalar(
                    select(AuditArtifact).where(
                        AuditArtifact.audit_report_id == report.id,
                        AuditArtifact.kind == AuditArtifactKind.CORE,
                        AuditArtifact.revision == 1,
                    )
                )
        if artifact is None:
            return ResolveResult(
                ResolveStatus.ARTIFACT_MISSING,
                cycle_id=cycle.id,
                report_id=report.id,
                report_date=cycle.report_date,
                notification_chat_id=cycle.notification_chat_id,
            )
        return ResolveResult(
            ResolveStatus.READY,
            cycle_id=cycle.id,
            report_id=report.id,
            artifact_id=artifact.id,
            artifact_kind=artifact.kind,
            artifact_revision=artifact.revision,
            report_date=cycle.report_date,
            input_hash=report.input_hash,
            notification_chat_id=cycle.notification_chat_id,
        )


def _money(value: object) -> str:
    if value is None:
        return "нет данных"
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "нет данных"
    return f"{decimal:,.2f}".replace(",", " ").replace(".", ",")


def _split_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        boundary = max(remaining.rfind("\n", 0, limit), remaining.rfind(" ", 0, limit))
        if boundary <= 0:
            boundary = limit
        parts.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


def format_report_summary_messages(
    summary_json: Mapping[str, Any] | None,
    *,
    kind_label: str = "CORE",
    start_index: int = 0,
) -> list[str]:
    """Format plain-text Telegram summaries, safely falling back for old JSON."""
    summary = summary_json or {}
    metrics = summary.get("company_metrics")
    lines = [f"Дебиторка · {kind_label}"]
    if isinstance(metrics, Mapping) and metrics:
        debt = metrics.get("total_debt")
        if isinstance(debt, Mapping):
            current = debt.get("current")
            previous = debt.get("previous")
            delta = debt.get("abs_delta")
            lines.append(f"Текущий долг: {_money(current)}")
            if previous is None:
                lines.append("Сравнение с предыдущим периодом: нет данных")
            else:
                try:
                    delta_decimal = Decimal(str(delta)) if delta is not None else None
                except (InvalidOperation, ValueError):
                    delta_decimal = None
                if delta_decimal is None or delta_decimal == 0:
                    lines.append("Изменение долга: без изменений")
                elif delta_decimal < 0:
                    lines.append(
                        f"Изменение долга: чистое снижение долга на {_money(-delta_decimal)}"
                    )
                else:
                    lines.append(
                        f"Изменение долга: рост долга на {_money(delta_decimal)}"
                    )
        overdue = summary.get("total_overdue")
        if isinstance(overdue, Mapping):
            lines.append(f"Общая просрочка: {_money(overdue.get('current'))}")
        for label, key in (
            ("Новые позиции", "new_count"),
            ("Закрытые позиции", "closed_count"),
            ("Изменения профиля просрочки", "overdue_profile_change_count"),
        ):
            if key in summary:
                lines.append(f"{label}: {summary[key]}")
    else:
        lines.append("Сводные показатели доступны в приложенном файле.")
    controls = summary.get("control_failures")
    if isinstance(controls, list) and controls:
        lines.append(f"Контрольные проверки с отклонениями: {len(controls)}")

    messages = _split_text("\n".join(lines))
    return messages[start_index:]


def format_report_caption(
    *, report_date: dt.date, kind: AuditArtifactKind | str, revision: int = 1
) -> str:
    kind_value = kind.value if isinstance(kind, AuditArtifactKind) else str(kind)
    label = kind_value.upper()
    suffix = "" if kind_value.lower() == AuditArtifactKind.CORE.value else f" r{revision}"
    return f"Дебиторка {report_date.isoformat()} · {label}{suffix}"[:TELEGRAM_CAPTION_LIMIT]


def build_report_filename(
    *, report_date: dt.date, kind: AuditArtifactKind | str, revision: int = 1
) -> str:
    kind_value = kind.value if isinstance(kind, AuditArtifactKind) else str(kind)
    label = kind_value.upper()
    revision_suffix = "" if kind_value.lower() == AuditArtifactKind.CORE.value else f"_r{revision}"
    return f"Дебиторка_{report_date.isoformat()}_{label}{revision_suffix}.xlsx"


async def create_manual_delivery(
    session: AsyncSession,
    *,
    artifact_id: int,
    destination_chat_id: int,
    requested_by_user_id: int,
) -> int:
    """Create an independent manual lifecycle, even for a previously sent artifact.

    Returns the new delivery id (ORM rows expire on commit of this short txn).
    """
    async with session.begin():
        delivery = ReportDelivery(
            audit_artifact_id=artifact_id,
            channel=ReportDeliveryChannel.TELEGRAM,
            kind=ReportDeliveryKind.MANUAL,
            status=ReportDeliveryStatus.PENDING,
            destination_chat_id=destination_chat_id,
            requested_by_user_id=requested_by_user_id,
        )
        session.add(delivery)
        await session.flush()
        return int(delivery.id)


async def load_delivery_send_context(
    session: AsyncSession, *, delivery_id: int
) -> DeliverySendContext | None:
    """Load the immutable bytes and resumable progress for a claimed delivery."""
    async with session.begin():
        delivery = await session.scalar(
            select(ReportDelivery).where(ReportDelivery.id == delivery_id)
        )
        if delivery is None:
            return None
        artifact = await session.scalar(
            select(AuditArtifact).where(AuditArtifact.id == delivery.audit_artifact_id)
        )
        if artifact is None:
            return None
        report = await session.scalar(
            select(AuditReport).where(AuditReport.id == artifact.audit_report_id)
        )
        if report is None:
            return None
        cycle = await session.scalar(
            select(AuditCycle).where(AuditCycle.id == report.audit_cycle_id)
        )
        if cycle is None:
            return None
        return DeliverySendContext(
            delivery_id=delivery.id,
            document_message_id=delivery.document_message_id,
            summary_sent_count=int(delivery.summary_sent_count),
            excel_bytes=bytes(artifact.excel_bytes or b""),
            excel_sha256=str(artifact.excel_sha256),
            artifact_kind=artifact.kind,
            artifact_revision=int(artifact.revision),
            report_date=cycle.report_date,
            summary_json=dict(report.summary_json or {}) if report.summary_json else None,
            summary_messages=format_report_summary_messages(
                report.summary_json,
                kind_label=artifact.kind.value.upper(),
                start_index=int(delivery.summary_sent_count),
            ),
            caption=format_report_caption(
                report_date=cycle.report_date,
                kind=artifact.kind,
                revision=artifact.revision,
            ),
            filename=build_report_filename(
                report_date=cycle.report_date,
                kind=artifact.kind,
                revision=artifact.revision,
            ),
        )
