"""Pure idle / reminder / expire policy for collecting AuditCycle rows."""
from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass


class IdleAction(str, enum.Enum):
    SKIP = "skip"
    REMIND = "remind"
    EXPIRE = "expire"


@dataclass(frozen=True)
class IdleTimeouts:
    idle_seconds: int
    reminder_interval_seconds: int
    max_reminders: int
    expire_grace_seconds: int


def decide_idle_action(
    *,
    now_utc: dt.datetime,
    last_activity_at: dt.datetime,
    reminder_count: int,
    last_reminder_at: dt.datetime | None,
    timeouts: IdleTimeouts,
) -> IdleAction:
    """Decide SKIP / REMIND / EXPIRE for a COLLECTING cycle (caller filters status).

    All datetimes must be timezone-aware UTC (or convertible). Never returns a path
    that would mark an incomplete cycle COMPLETED.
    """
    now = _ensure_utc(now_utc)
    activity = _ensure_utc(last_activity_at)
    last_reminder = (
        _ensure_utc(last_reminder_at) if last_reminder_at is not None else None
    )

    if reminder_count < 0:
        raise ValueError("reminder_count must be >= 0")
    if timeouts.max_reminders < 1:
        raise ValueError("max_reminders must be >= 1")

    if reminder_count == 0:
        if (now - activity).total_seconds() < timeouts.idle_seconds:
            return IdleAction.SKIP
        return IdleAction.REMIND

    if reminder_count < timeouts.max_reminders:
        if last_reminder is None:
            return IdleAction.REMIND
        if (now - last_reminder).total_seconds() < timeouts.reminder_interval_seconds:
            return IdleAction.SKIP
        return IdleAction.REMIND

    # reminder_count >= max_reminders → grace then expire
    if last_reminder is None:
        return IdleAction.EXPIRE
    if (now - last_reminder).total_seconds() < timeouts.expire_grace_seconds:
        return IdleAction.SKIP
    return IdleAction.EXPIRE


def _ensure_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)
