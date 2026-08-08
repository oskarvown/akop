"""Unit tests for Stage 4.2 CORE workbook builder."""
from __future__ import annotations

import datetime as dt
import time
from decimal import Decimal
from io import BytesIO

import openpyxl

from app.application.comparison_service import (
    ControlCollision,
    ControlEquality,
    CycleComparison,
    MatchedEntity,
    MetricDelta,
    PositionSnapshot,
    compute_metric_deltas,
)
from app.application.report_workbook import (
    SHEET_ORDER,
    build_core_excel_bytes,
    change_class,
    sanitize_excel_text,
)


def _metrics(total: Decimal | None = Decimal("100")) -> dict[str, Decimal | None]:
    return {
        "document_amount": Decimal("0"),
        "total_debt": total,
        "advance": Decimal("0"),
        "not_due": Decimal("0"),
        "overdue_1_7": Decimal("0"),
        "overdue_8_14": Decimal("0"),
        "overdue_15_21": Decimal("0"),
        "overdue_22_30": Decimal("0"),
        "overdue_over_31": Decimal("0"),
    }


def _pos(
    *,
    pid: int,
    key: str,
    level: int = 1,
    total: Decimal | None = Decimal("100"),
    label: str = "Acme",
    department: str = "regional",
    manager_group_id: int = 1,
    counterparty_id: int = 1,
    parent_position_id: int | None = None,
    manager_group_name: str = "Ivanov",
    counterparty_name: str = "Acme LLC",
) -> PositionSnapshot:
    return PositionSnapshot(
        id=pid,
        match_key=key,
        match_key_hash="h" * 64,
        outline_level=level,
        raw_label=label,
        counterparty_id=counterparty_id,
        manager_group_id=manager_group_id,
        source_file_id=1,
        department=department,
        metrics=_metrics(total),
        credit_limit=None,
        parent_position_id=parent_position_id,
        manager_group_name=manager_group_name,
        counterparty_name=counterparty_name,
    )


def _entity(
    curr: PositionSnapshot | None,
    prev: PositionSnapshot | None,
    *,
    ambiguous: bool = False,
) -> MatchedEntity:
    level = (curr or prev).outline_level  # type: ignore[union-attr]
    key = (curr or prev).match_key  # type: ignore[union-attr]
    if ambiguous:
        deltas = {
            name: MetricDelta(None, None, None, None)
            for name in _metrics()
        }
    else:
        deltas = compute_metric_deltas(curr=curr, prev=prev)
    return MatchedEntity(
        match_key=key,
        outline_level=level,
        current=curr,
        previous=prev,
        ambiguous=ambiguous,
        collision_current_count=2 if ambiguous else (1 if curr else 0),
        collision_previous_count=0 if ambiguous else (1 if prev else 0),
        deltas=deltas,
        overdue_profile_changed=False,
        document_bucket_transition=None,
    )


def _comparison(entities: list[MatchedEntity]) -> CycleComparison:
    return CycleComparison(
        current_cycle_id=10,
        current_report_date=dt.date(2026, 8, 8),
        previous_cycle_id=9,
        previous_report_date=dt.date(2026, 8, 1),
        entities=tuple(entities),
        collisions=(),
        control_equalities=(
            ControlEquality(
                name="company_total_debt_vs_sum_departments",
                left=Decimal("100"),
                right=Decimal("100"),
                ok=True,
            ),
        ),
        ambiguous_keys=frozenset(),
    )


def test_sanitize_formula_injection() -> None:
    assert sanitize_excel_text("=CMD()") == "'=CMD()"
    assert sanitize_excel_text("+1+1") == "'+1+1"
    assert sanitize_excel_text("-1") == "'-1"
    assert sanitize_excel_text("@SUM(A1)") == "'@SUM(A1)"
    assert sanitize_excel_text("\tHIJACK") == "'\tHIJACK"
    assert sanitize_excel_text("normal") == "normal"


def test_change_class_labels() -> None:
    curr = _pos(pid=1, key="c:1", total=Decimal("120"))
    prev = _pos(pid=2, key="c:1", total=Decimal("100"))
    assert "рост" in change_class(_entity(curr, prev))
    assert "чистое снижение" in change_class(
        _entity(
            _pos(pid=1, key="c:1", total=Decimal("50")),
            _pos(pid=2, key="c:1", total=Decimal("100")),
        )
    )
    assert change_class(_entity(curr, None)) == "новая позиция"
    assert change_class(_entity(None, prev)) == "закрыта"


def test_workbook_has_eight_sheets_and_sanitizes_labels() -> None:
    evil = _pos(pid=1, key="c:1", label="=1+1", total=Decimal("100"))
    prev = _pos(pid=2, key="c:1", label="=1+1", total=Decimal("80"))
    raw, digest = build_core_excel_bytes(_comparison([_entity(evil, prev)]))
    assert len(digest) == 64
    wb = openpyxl.load_workbook(BytesIO(raw))
    assert wb.sheetnames == list(SHEET_ORDER)
    # Label cell on counterparties sheet should be escaped
    ws = wb["Контрагенты"]
    labels = [ws.cell(r, 5).value for r in range(2, ws.max_row + 1)]
    assert any(str(v).startswith("'=") for v in labels if v)


def test_rebuild_same_sha() -> None:
    curr = _pos(pid=1, key="c:1", total=Decimal("150"))
    prev = _pos(pid=2, key="c:1", total=Decimal("100"))
    comparison = _comparison([_entity(curr, prev)])
    _, sha1 = build_core_excel_bytes(comparison)
    time.sleep(1.05)
    _, sha2 = build_core_excel_bytes(comparison)
    assert sha1 == sha2


def test_control_sheet_includes_collision() -> None:
    curr = _pos(pid=1, key="c:dup", label="A")
    comparison = CycleComparison(
        current_cycle_id=1,
        current_report_date=dt.date(2026, 8, 8),
        previous_cycle_id=None,
        previous_report_date=None,
        entities=(_entity(curr, None, ambiguous=True),),
        collisions=(
            ControlCollision(
                match_key="c:dup",
                cycle_side="current",
                count=2,
                raw_labels=("A", "B"),
            ),
        ),
        control_equalities=(),
        ambiguous_keys=frozenset({"c:dup"}),
    )
    raw, _ = build_core_excel_bytes(comparison)
    wb = openpyxl.load_workbook(BytesIO(raw))
    control = wb["Контроль"]
    types = [control.cell(r, 1).value for r in range(2, control.max_row + 1)]
    assert "collision" in types
