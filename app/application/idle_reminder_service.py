"""Claim / record / expire idle reminders for collecting AuditCycle rows."""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import cycle_status_summary
from app.application.idle_policy import IdleAction, IdleTimeouts, decide_idle_action
from app.domain.enums import Department
from app.domain.models import AuditCycle, AuditCycleStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReminderClaim:
    cycle_id: int
    claim_token: uuid.UUID
    notification_chat_id: int
    report_date: dt.date
    reminder_count: int
    present: frozenset[Department]
    missing: frozenset[Department]


def _timeouts_from_settings(settings: object) -> IdleTimeouts:
    return IdleTimeouts(
        idle_seconds=int(settings.audit_idle_timeout_seconds),  # type: ignore[attr-defined]
        reminder_interval_seconds=int(
            settings.audit_reminder_interval_seconds  # type: ignore[attr-defined]
        ),
        max_reminders=int(settings.audit_max_reminders),  # type: ignore[attr-defined]
        expire_grace_seconds=int(settings.audit_expire_grace_seconds),  # type: ignore[attr-defined]
    )


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


async def list_collecting_cycle_ids(session: AsyncSession) -> list[int]:
    async with session.begin():
        rows = (
            await session.execute(
                select(AuditCycle.id)
                .where(AuditCycle.status == AuditCycleStatus.COLLECTING)
                .order_by(AuditCycle.id)
            )
        ).scalars().all()
        return list(rows)


async def claim_reminder(
    session: AsyncSession,
    *,
    cycle_id: int,
    settings: object,
    now_utc: dt.datetime | None = None,
) -> ReminderClaim | None:
    """Atomically claim a due REMIND cycle. Does not increment reminder_count."""
    now = _ensure_utc(now_utc or dt.datetime.now(dt.timezone.utc))
    claim_ttl = int(settings.audit_reminder_claim_ttl_seconds)  # type: ignore[attr-defined]
    timeouts = _timeouts_from_settings(settings)

    async with session.begin():
        cycle = await session.scalar(
            select(AuditCycle).where(AuditCycle.id == cycle_id).with_for_update()
        )
        if cycle is None or cycle.status != AuditCycleStatus.COLLECTING:
            return None

        if _claim_is_active(
            token=cycle.reminder_claim_token,
            claimed_at=cycle.reminder_claimed_at,
            now_utc=now,
            claim_ttl_seconds=claim_ttl,
        ):
            return None

        action = decide_idle_action(
            now_utc=now,
            last_activity_at=cycle.last_activity_at,
            reminder_count=cycle.reminder_count,
            last_reminder_at=cycle.last_reminder_at,
            timeouts=timeouts,
        )
        if action != IdleAction.REMIND:
            return None

        token = uuid.uuid4()
        cycle.reminder_claim_token = token
        cycle.reminder_claimed_at = func.clock_timestamp()
        await session.flush()

        summary = await cycle_status_summary(session, cycle.id)
        return ReminderClaim(
            cycle_id=cycle.id,
            claim_token=token,
            notification_chat_id=cycle.notification_chat_id,
            report_date=cycle.report_date,
            reminder_count=cycle.reminder_count,
            present=summary.present,
            missing=summary.missing,
        )


async def claim_still_valid(
    session: AsyncSession,
    *,
    cycle_id: int,
    claim_token: uuid.UUID,
) -> bool:
    """Short recheck before Telegram send (no long-held lock across API)."""
    async with session.begin():
        row = (
            await session.execute(
                select(
                    AuditCycle.status,
                    AuditCycle.reminder_claim_token,
                ).where(AuditCycle.id == cycle_id)
            )
        ).one_or_none()
        if row is None:
            return False
        return (
            row.status == AuditCycleStatus.COLLECTING
            and row.reminder_claim_token == claim_token
        )


async def record_successful_reminder(
    session: AsyncSession,
    *,
    cycle_id: int,
    claim_token: uuid.UUID,
    expected_reminder_count: int,
) -> bool:
    """Increment reminder_count only when claim token still matches.

    Activity clears the claim token, so a late record after add/replace/undo is a
    no-op even if Telegram already delivered a stale reminder (known race).
    """
    async with session.begin():
        cycle = await session.scalar(
            select(AuditCycle).where(AuditCycle.id == cycle_id).with_for_update()
        )
        if cycle is None:
            return False
        if cycle.status != AuditCycleStatus.COLLECTING:
            return False
        if cycle.reminder_claim_token != claim_token:
            return False
        if cycle.reminder_count != expected_reminder_count:
            return False

        cycle.reminder_count = cycle.reminder_count + 1
        cycle.last_reminder_at = func.clock_timestamp()
        cycle.reminder_claim_token = None
        cycle.reminder_claimed_at = None
        return True


async def release_reminder_claim(
    session: AsyncSession,
    *,
    cycle_id: int,
    claim_token: uuid.UUID,
) -> bool:
    """Clear own claim after Telegram failure; does not touch reminder_count."""
    async with session.begin():
        cycle = await session.scalar(
            select(AuditCycle).where(AuditCycle.id == cycle_id).with_for_update()
        )
        if cycle is None:
            return False
        if cycle.reminder_claim_token != claim_token:
            return False
        cycle.reminder_claim_token = None
        cycle.reminder_claimed_at = None
        return True


async def expire_cycle_if_due(
    session: AsyncSession,
    *,
    cycle_id: int,
    settings: object,
    now_utc: dt.datetime | None = None,
) -> bool:
    """COLLECTING → EXPIRED when policy says so. Never COMPLETED."""
    now = _ensure_utc(now_utc or dt.datetime.now(dt.timezone.utc))
    claim_ttl = int(settings.audit_reminder_claim_ttl_seconds)  # type: ignore[attr-defined]
    timeouts = _timeouts_from_settings(settings)

    async with session.begin():
        cycle = await session.scalar(
            select(AuditCycle).where(AuditCycle.id == cycle_id).with_for_update()
        )
        if cycle is None or cycle.status != AuditCycleStatus.COLLECTING:
            return False

        if _claim_is_active(
            token=cycle.reminder_claim_token,
            claimed_at=cycle.reminder_claimed_at,
            now_utc=now,
            claim_ttl_seconds=claim_ttl,
        ):
            return False

        action = decide_idle_action(
            now_utc=now,
            last_activity_at=cycle.last_activity_at,
            reminder_count=cycle.reminder_count,
            last_reminder_at=cycle.last_reminder_at,
            timeouts=timeouts,
        )
        if action != IdleAction.EXPIRE:
            return False

        summary = await cycle_status_summary(session, cycle.id)
        if summary.is_complete:
            logger.warning(
                "Refuse EXPIRE for complete collecting cycle_id=%s", cycle_id
            )
            return False

        cycle.status = AuditCycleStatus.EXPIRED
        cycle.expired_at = func.clock_timestamp()
        cycle.reminder_claim_token = None
        cycle.reminder_claimed_at = None
        return True


async def process_cycle_decision(
    session: AsyncSession,
    *,
    cycle_id: int,
    settings: object,
    now_utc: dt.datetime | None = None,
) -> IdleAction:
    """Peek policy without claiming."""
    now = _ensure_utc(now_utc or dt.datetime.now(dt.timezone.utc))
    timeouts = _timeouts_from_settings(settings)
    async with session.begin():
        cycle = await session.scalar(
            select(AuditCycle).where(AuditCycle.id == cycle_id)
        )
        if cycle is None or cycle.status != AuditCycleStatus.COLLECTING:
            return IdleAction.SKIP
        return decide_idle_action(
            now_utc=now,
            last_activity_at=cycle.last_activity_at,
            reminder_count=cycle.reminder_count,
            last_reminder_at=cycle.last_reminder_at,
            timeouts=timeouts,
        )
