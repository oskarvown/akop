"""Shared formatting for /status and idle reminder messages."""
from __future__ import annotations

import datetime as dt

from app.application.audit_service import CycleStatusSummary, CycleStatusView
from app.bot.keyboards.department import DEPARTMENT_LABELS
from app.domain.models import AuditCycleStatus


def format_missing_departments(summary: CycleStatusSummary) -> str:
    missing = ", ".join(
        DEPARTMENT_LABELS[item]
        for item in sorted(summary.missing, key=lambda item: item.value)
    )
    return missing or "—"


def format_present_departments(summary: CycleStatusSummary) -> str:
    present = ", ".join(
        DEPARTMENT_LABELS[item]
        for item in sorted(summary.present, key=lambda item: item.value)
    )
    return present or "—"


def format_cycle_block(cycle: CycleStatusView) -> str:
    if cycle.status == AuditCycleStatus.COLLECTING:
        return "\n".join(
            [
                (
                    f"Сбор за {cycle.report_date:%d.%m.%Y}: "
                    f"{len(cycle.summary.present)}/5"
                ),
                f"Получены: {format_present_departments(cycle.summary)}",
                f"Не хватает: {format_missing_departments(cycle.summary)}",
            ]
        )
    if cycle.status == AuditCycleStatus.EXPIRED:
        return (
            f"Просрочен {cycle.report_date:%d.%m.%Y}: "
            f"{len(cycle.summary.present)}/5, не хватает: "
            f"{format_missing_departments(cycle.summary)}"
        )
    return (
        f"Завершён {cycle.report_date:%d.%m.%Y}: "
        f"5/5, общий долг {cycle.total_debt:,.2f}"
    )


def format_reminder_message(
    *,
    report_date: dt.date,
    present_count: int,
    missing_labels: tuple[str, ...] | list[str],
) -> str:
    missing = ", ".join(missing_labels) if missing_labels else "—"
    return (
        f"Аудит за {report_date:%d.%m.%Y}: {present_count}/5. "
        f"Не хватает: {missing}."
    )
