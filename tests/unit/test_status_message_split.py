"""Unit tests for /status message packing."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.application.audit_service import CycleStatusSummary, CycleStatusView
from app.bot.handlers.status import format_cycle_block, split_status_messages
from app.domain.enums import Department
from app.domain.models import AuditCycleStatus


def _collecting(day: int) -> CycleStatusView:
    present = frozenset({Department.REGIONAL})
    return CycleStatusView(
        id=day,
        report_date=dt.date(2025, 7, day),
        status=AuditCycleStatus.COLLECTING,
        completed_at=None,
        summary=CycleStatusSummary(
            present=present,
            missing=frozenset(Department) - present,
        ),
        total_debt=Decimal("0"),
    )


def test_split_status_messages_packs_cycles_under_telegram_limit() -> None:
    cycles = [_collecting(day) for day in range(1, 31)]
    messages = split_status_messages(cycles, limit=500)

    assert len(messages) > 1
    assert all(len(message) <= 500 for message in messages)
    joined = "\n\n".join(messages)
    for cycle in cycles:
        assert format_cycle_block(cycle) in joined


def test_split_status_messages_empty() -> None:
    assert split_status_messages([]) == ["Нет активных аудитов."]
