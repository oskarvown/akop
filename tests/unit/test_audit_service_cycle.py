import datetime as dt

import pytest

from app.application.audit_service import (
    CycleImmutableError,
    CycleStatusSummary,
    assert_cycle_mutable,
)
from app.domain.enums import Department
from app.domain.models import AuditCycle, AuditCycleStatus


def test_summary_is_complete_only_for_strict_five_of_five() -> None:
    partial = CycleStatusSummary(
        present=frozenset(list(Department)[:4]),
        missing=frozenset({list(Department)[4]}),
    )
    complete = CycleStatusSummary(
        present=frozenset(Department),
        missing=frozenset(),
    )

    assert partial.is_complete is False
    assert complete.is_complete is True


@pytest.mark.parametrize(
    "status",
    [AuditCycleStatus.COMPLETED, AuditCycleStatus.EXPIRED],
)
def test_assert_cycle_mutable_rejects_every_non_collecting_status(
    status: AuditCycleStatus,
) -> None:
    cycle = AuditCycle(report_date=dt.date(2026, 7, 30), status=status, notification_chat_id=743971617)

    with pytest.raises(CycleImmutableError):
        assert_cycle_mutable(cycle)


def test_assert_cycle_mutable_accepts_collecting() -> None:
    cycle = AuditCycle(
        notification_chat_id=743971617,
        report_date=dt.date(2026, 7, 30),
        status=AuditCycleStatus.COLLECTING,
    )

    assert_cycle_mutable(cycle)
