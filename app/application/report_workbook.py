"""CORE Excel workbook builder (Stage 4.2).

Deterministic openpyxl export: formula-injection safe labels, fixed ZIP/core
metadata so rebuild with the same comparison yields the same excel_sha256.
"""
from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from io import BytesIO
import re
from typing import Sequence

from openpyxl import Workbook
from openpyxl.packaging.core import DocumentProperties
from openpyxl.utils import get_column_letter
from openpyxl.workbook.workbook import Workbook as WorkbookType

from app.application.comparison_service import (
    ADDITIVE_METRICS,
    CycleComparison,
    MatchedEntity,
    PositionSnapshot,
    abs_delta,
    percent_delta,
)

SHEET_SUMMARY = "Сводка"
SHEET_DEPARTMENTS = "Отделы"
SHEET_MANAGER_GROUPS = "ManagerGroup"
SHEET_COUNTERPARTIES = "Контрагенты"
SHEET_CONTRACTS = "Договоры"
SHEET_DOCUMENTS = "Документы"
SHEET_CHANGES = "Изменения"
SHEET_CONTROL = "Контроль"

SHEET_ORDER: tuple[str, ...] = (
    SHEET_SUMMARY,
    SHEET_DEPARTMENTS,
    SHEET_MANAGER_GROUPS,
    SHEET_COUNTERPARTIES,
    SHEET_CONTRACTS,
    SHEET_DOCUMENTS,
    SHEET_CHANGES,
    SHEET_CONTROL,
)

MONEY_FORMAT = "#,##0.00"
ND = "н/д"
CREATOR = "debitor-bot"
FIXED_TIMESTAMP = datetime(2020, 1, 1, 0, 0, 0)
ZIP_DATE_TIME = (2020, 1, 1, 0, 0, 0)
FIXED_CORE_ISO = "2020-01-01T00:00:00Z"

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

DEPARTMENT_LABELS: dict[str, str] = {
    "szfo_1": "СЗФО-1",
    "szfo_2": "СЗФО-2",
    "regional": "Региональный",
    "moscow": "Москва",
    "fokin": "Фокин",
}

CHANGE_NEW = "новая позиция"
CHANGE_CLOSED = "закрыта"
CHANGE_GROWTH = "рост долга"
CHANGE_DECLINE = "чистое снижение долга"
CHANGE_UNCHANGED = "без изменения total_debt"
CHANGE_AMBIGUOUS = "ambiguous collision"
CHANGE_OVERDUE = "изменение профиля просрочки"


def sanitize_excel_text(value: str | None) -> str:
    """Neutralize formula injection for labels / names written as cell text."""
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def format_decimal(value: Decimal | None) -> Decimal | str | None:
    if value is None:
        return None
    return value


def format_percent(value: Decimal | None) -> Decimal | str:
    if value is None:
        return ND
    return value


def _abs_rank(delta: Decimal | None) -> Decimal:
    if delta is None:
        return Decimal("-1")
    return abs(delta)


def change_class(entity: MatchedEntity) -> str:
    if entity.ambiguous:
        return CHANGE_AMBIGUOUS
    if entity.current is not None and entity.previous is None:
        return CHANGE_NEW
    if entity.current is None and entity.previous is not None:
        return CHANGE_CLOSED
    debt = entity.deltas["total_debt"].abs_delta
    if debt is None:
        base = CHANGE_UNCHANGED
    elif debt > 0:
        base = CHANGE_GROWTH
    elif debt < 0:
        base = CHANGE_DECLINE
    else:
        base = CHANGE_UNCHANGED
    if entity.overdue_profile_changed:
        return f"{base}; {CHANGE_OVERDUE}"
    return base


def _entity_display(entity: MatchedEntity) -> tuple[str, str, str, str]:
    snap = entity.current or entity.previous
    assert snap is not None
    dept = DEPARTMENT_LABELS.get(snap.department, snap.department)
    return (
        dept,
        snap.manager_group_name or str(snap.manager_group_id),
        snap.counterparty_name or str(snap.counterparty_id),
        snap.raw_label,
    )


def _sort_entities(entities: Sequence[MatchedEntity]) -> list[MatchedEntity]:
    return sorted(
        entities,
        key=lambda e: (
            -_abs_rank(e.deltas["total_debt"].abs_delta),
            e.match_key,
        ),
    )


def _positions_by_id(
    entities: Sequence[MatchedEntity],
) -> dict[int, PositionSnapshot]:
    out: dict[int, PositionSnapshot] = {}
    for entity in entities:
        for snap in (entity.current, entity.previous):
            if snap is not None:
                out[snap.id] = snap
    return out


def _ancestor_label(
    snap: PositionSnapshot | None,
    *,
    by_id: dict[int, PositionSnapshot],
    outline_level: int,
) -> str:
    cur = snap
    while cur is not None:
        if cur.outline_level == outline_level:
            return cur.raw_label
        if cur.parent_position_id is None:
            break
        cur = by_id.get(cur.parent_position_id)
    return ""


@dataclass(frozen=True)
class AggregateRow:
    key: str
    label: str
    department: str
    current_total: Decimal | None
    previous_total: Decimal | None
    abs_delta: Decimal | None
    percent_delta: Decimal | None


def _sum_l1_total(
    entities: Sequence[MatchedEntity],
    *,
    side: str,
    predicate,
) -> Decimal | None:
    total = Decimal("0")
    any_value = False
    for entity in entities:
        if entity.outline_level != 1 or entity.ambiguous:
            continue
        snap = entity.current if side == "current" else entity.previous
        if snap is None or not predicate(snap):
            continue
        value = snap.metrics.get("total_debt")
        if value is None:
            continue
        total += value
        any_value = True
    return total if any_value else None


def build_department_rows(comparison: CycleComparison) -> list[AggregateRow]:
    depts = sorted(
        {
            (e.current or e.previous).department
            for e in comparison.entities
            if (e.current or e.previous) is not None
        }
    )
    rows: list[AggregateRow] = []
    for dept in depts:
        curr = _sum_l1_total(
            comparison.entities,
            side="current",
            predicate=lambda s, d=dept: s.department == d,
        )
        prev = _sum_l1_total(
            comparison.entities,
            side="previous",
            predicate=lambda s, d=dept: s.department == d,
        )
        curr_present = curr is not None or any(
            e.outline_level == 1
            and not e.ambiguous
            and e.current is not None
            and e.current.department == dept
            for e in comparison.entities
        )
        prev_present = prev is not None or any(
            e.outline_level == 1
            and not e.ambiguous
            and e.previous is not None
            and e.previous.department == dept
            for e in comparison.entities
        )
        # Presence for abs_delta: department present if any L1 on that side.
        ad = abs_delta(
            curr_present=curr_present,
            prev_present=prev_present,
            curr_m=curr if curr_present else None,
            prev_m=prev if prev_present else None,
        )
        # When present but all NULL totals, curr/prev stay None — abs_delta None.
        if curr_present and curr is None and prev_present and prev is None:
            ad = None
        elif curr_present and curr is None and not prev_present:
            ad = None
        elif prev_present and prev is None and not curr_present:
            ad = None
        rows.append(
            AggregateRow(
                key=dept,
                label=DEPARTMENT_LABELS.get(dept, dept),
                department=dept,
                current_total=curr,
                previous_total=prev,
                abs_delta=ad,
                percent_delta=percent_delta(ad, prev),
            )
        )
    return sorted(rows, key=lambda r: (-_abs_rank(r.abs_delta), r.key))


def build_manager_group_rows(comparison: CycleComparison) -> list[AggregateRow]:
    groups: dict[int, str] = {}
    depts: dict[int, str] = {}
    for entity in comparison.entities:
        snap = entity.current or entity.previous
        if snap is None:
            continue
        groups[snap.manager_group_id] = snap.manager_group_name or str(
            snap.manager_group_id
        )
        depts[snap.manager_group_id] = snap.department
    rows: list[AggregateRow] = []
    for mg_id in sorted(groups):
        curr = _sum_l1_total(
            comparison.entities,
            side="current",
            predicate=lambda s, mid=mg_id: s.manager_group_id == mid,
        )
        prev = _sum_l1_total(
            comparison.entities,
            side="previous",
            predicate=lambda s, mid=mg_id: s.manager_group_id == mid,
        )
        curr_present = any(
            e.outline_level == 1
            and not e.ambiguous
            and e.current is not None
            and e.current.manager_group_id == mg_id
            for e in comparison.entities
        )
        prev_present = any(
            e.outline_level == 1
            and not e.ambiguous
            and e.previous is not None
            and e.previous.manager_group_id == mg_id
            for e in comparison.entities
        )
        ad = abs_delta(
            curr_present=curr_present,
            prev_present=prev_present,
            curr_m=curr if curr_present else None,
            prev_m=prev if prev_present else None,
        )
        if curr_present and curr is None and prev_present and prev is None:
            ad = None
        rows.append(
            AggregateRow(
                key=str(mg_id),
                label=groups[mg_id],
                department=depts[mg_id],
                current_total=curr,
                previous_total=prev,
                abs_delta=ad,
                percent_delta=percent_delta(ad, prev),
            )
        )
    return sorted(rows, key=lambda r: (-_abs_rank(r.abs_delta), r.key))


def _write_header(ws, headers: Sequence[str]) -> None:
    for col, title in enumerate(headers, start=1):
        ws.cell(1, col, title)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"


def _set_money(cell) -> None:
    cell.number_format = MONEY_FORMAT


def _write_money(ws, row: int, col: int, value: Decimal | None):
    cell = ws.cell(row, col, format_decimal(value))
    if value is not None:
        _set_money(cell)
    return cell


def _write_pct(ws, row: int, col: int, value: Decimal | None) -> None:
    cell = ws.cell(row, col, format_percent(value))
    if value is not None:
        cell.number_format = "0.00"


def _apply_column_widths(ws, widths: Sequence[float]) -> None:
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width


def _normalize_xlsx_bytes(raw: bytes) -> bytes:
    """Rewrite ZIP entry timestamps and core created/modified for stable SHA."""
    src = zipfile.ZipFile(BytesIO(raw), "r")
    out = BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        for info in sorted(src.infolist(), key=lambda item: item.filename):
            data = src.read(info.filename)
            if info.filename == "docProps/core.xml":
                text = data.decode("utf-8")
                text = re.sub(
                    r"(<(?:dcterms:)?created[^>]*>)([^<]+)(</(?:dcterms:)?created>)",
                    rf"\g<1>{FIXED_CORE_ISO}\g<3>",
                    text,
                )
                text = re.sub(
                    r"(<(?:dcterms:)?modified[^>]*>)([^<]+)(</(?:dcterms:)?modified>)",
                    rf"\g<1>{FIXED_CORE_ISO}\g<3>",
                    text,
                )
                data = text.encode("utf-8")
            new_info = zipfile.ZipInfo(filename=info.filename, date_time=ZIP_DATE_TIME)
            new_info.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(new_info, data)
    src.close()
    return out.getvalue()


def _prepare_workbook() -> WorkbookType:
    wb = Workbook()
    # Remove default sheet; recreate in fixed order.
    default = wb.active
    wb.remove(default)
    for name in SHEET_ORDER:
        wb.create_sheet(name)
    wb.properties = DocumentProperties(
        creator=CREATOR,
        lastModifiedBy=CREATOR,
        created=FIXED_TIMESTAMP,
        modified=FIXED_TIMESTAMP,
    )
    return wb


def _fill_summary(ws, comparison: CycleComparison) -> None:
    headers = ("Показатель", "Значение")
    _write_header(ws, headers)
    rows = [
        ("current_cycle_id", comparison.current_cycle_id),
        ("current_report_date", comparison.current_report_date.isoformat()),
        (
            "previous_cycle_id",
            comparison.previous_cycle_id
            if comparison.previous_cycle_id is not None
            else ND,
        ),
        (
            "previous_report_date",
            comparison.previous_report_date.isoformat()
            if comparison.previous_report_date
            else ND,
        ),
        ("entity_count", len(comparison.entities)),
        ("ambiguous_key_count", len(comparison.ambiguous_keys)),
        ("collision_count", len(comparison.collisions)),
        (
            "overdue_profile_change_count",
            sum(1 for e in comparison.entities if e.overdue_profile_changed),
        ),
        (
            "control_failure_count",
            sum(
                1
                for c in comparison.control_equalities
                if not c.ok and not c.diagnostic
            ),
        ),
    ]
    company_curr = _sum_l1_total(
        comparison.entities, side="current", predicate=lambda _s: True
    )
    company_prev = _sum_l1_total(
        comparison.entities, side="previous", predicate=lambda _s: True
    )
    rows.append(("company_total_debt_current", company_curr if company_curr is not None else ND))
    rows.append(("company_total_debt_previous", company_prev if company_prev is not None else ND))
    for idx, (name, value) in enumerate(rows, start=2):
        ws.cell(idx, 1, name)
        cell = ws.cell(idx, 2, value if not isinstance(value, Decimal) else value)
        if isinstance(value, Decimal):
            _set_money(cell)
    _apply_column_widths(ws, (36, 24))
    # Extend autofilter to data rows
    ws.auto_filter.ref = f"A1:B{len(rows) + 1}"


def _fill_aggregate_sheet(ws, rows: Sequence[AggregateRow], *, label_header: str) -> None:
    headers = (
        label_header,
        "Отдел",
        "total_debt текущий",
        "total_debt предыдущий",
        "Δ total_debt",
        "% Δ total_debt",
    )
    _write_header(ws, headers)
    for r_idx, row in enumerate(rows, start=2):
        ws.cell(r_idx, 1, sanitize_excel_text(row.label))
        ws.cell(
            r_idx,
            2,
            sanitize_excel_text(DEPARTMENT_LABELS.get(row.department, row.department)),
        )
        _write_money(ws, r_idx, 3, row.current_total)
        _write_money(ws, r_idx, 4, row.previous_total)
        _write_money(ws, r_idx, 5, row.abs_delta)
        _write_pct(ws, r_idx, 6, row.percent_delta)
    last = max(len(rows) + 1, 1)
    ws.auto_filter.ref = f"A1:F{last}"
    _apply_column_widths(ws, (28, 16, 18, 18, 16, 14))


def _fill_entity_sheet(
    ws,
    entities: Sequence[MatchedEntity],
    *,
    by_id: dict[int, PositionSnapshot],
    include_object: bool,
    include_contract: bool,
) -> None:
    headers = [
        "match_key",
        "Отдел",
        "ManagerGroup",
        "Контрагент",
        "Ярлык",
    ]
    if include_contract:
        headers.append("Договор")
    if include_object:
        headers.append("Объект")
    headers.extend(
        [
            "класс изменения",
            "total_debt текущий",
            "total_debt предыдущий",
            "Δ total_debt",
            "% Δ total_debt",
            "изменение профиля просрочки",
            "переход корзины документа",
        ]
    )
    # Remaining additive metrics as current/prev/delta triples (compact: current only + delta)
    for metric in ADDITIVE_METRICS:
        if metric == "total_debt":
            continue
        headers.append(f"Δ {metric}")

    _write_header(ws, headers)
    sorted_entities = _sort_entities(entities)
    for r_idx, entity in enumerate(sorted_entities, start=2):
        dept, mg, cp, label = _entity_display(entity)
        snap = entity.current or entity.previous
        col = 1
        ws.cell(r_idx, col, entity.match_key)
        col += 1
        ws.cell(r_idx, col, sanitize_excel_text(dept))
        col += 1
        ws.cell(r_idx, col, sanitize_excel_text(mg))
        col += 1
        ws.cell(r_idx, col, sanitize_excel_text(cp))
        col += 1
        ws.cell(r_idx, col, sanitize_excel_text(label))
        col += 1
        if include_contract:
            contract = _ancestor_label(snap, by_id=by_id, outline_level=2)
            if entity.outline_level == 2 and snap is not None:
                contract = snap.raw_label
            ws.cell(r_idx, col, sanitize_excel_text(contract))
            col += 1
        if include_object:
            obj = _ancestor_label(snap, by_id=by_id, outline_level=3)
            if entity.outline_level == 3 and snap is not None:
                obj = snap.raw_label
            ws.cell(r_idx, col, sanitize_excel_text(obj))
            col += 1
        ws.cell(r_idx, col, change_class(entity))
        col += 1
        debt = entity.deltas["total_debt"]
        _write_money(ws, r_idx, col, debt.current)
        col += 1
        _write_money(ws, r_idx, col, debt.previous)
        col += 1
        _write_money(ws, r_idx, col, debt.abs_delta)
        col += 1
        _write_pct(ws, r_idx, col, debt.percent_delta)
        col += 1
        ws.cell(r_idx, col, "да" if entity.overdue_profile_changed else "нет")
        col += 1
        if entity.document_bucket_transition is None:
            ws.cell(r_idx, col, "")
        else:
            frm, to = entity.document_bucket_transition
            ws.cell(r_idx, col, f"{frm} → {to}")
        col += 1
        for metric in ADDITIVE_METRICS:
            if metric == "total_debt":
                continue
            _write_money(ws, r_idx, col, entity.deltas[metric].abs_delta)
            col += 1
    last_col = get_column_letter(len(headers))
    last_row = max(len(sorted_entities) + 1, 1)
    ws.auto_filter.ref = f"A1:{last_col}{last_row}"
    _apply_column_widths(ws, [18] * len(headers))


def _fill_changes(ws, comparison: CycleComparison) -> None:
    headers = (
        "match_key",
        "уровень",
        "класс изменения",
        "Отдел",
        "ManagerGroup",
        "Контрагент",
        "Ярлык",
        "Δ total_debt",
        "% Δ total_debt",
        "изменение профиля просрочки",
    )
    _write_header(ws, headers)
    # Only rows that are new/closed/nonzero debt delta/overdue profile/ambiguous.
    interesting: list[MatchedEntity] = []
    for entity in comparison.entities:
        if entity.ambiguous:
            interesting.append(entity)
            continue
        debt = entity.deltas["total_debt"].abs_delta
        if entity.current is None or entity.previous is None:
            interesting.append(entity)
            continue
        if debt is not None and debt != 0:
            interesting.append(entity)
            continue
        if entity.overdue_profile_changed:
            interesting.append(entity)
    interesting = _sort_entities(interesting)
    for r_idx, entity in enumerate(interesting, start=2):
        dept, mg, cp, label = _entity_display(entity)
        ws.cell(r_idx, 1, entity.match_key)
        ws.cell(r_idx, 2, entity.outline_level)
        ws.cell(r_idx, 3, change_class(entity))
        ws.cell(r_idx, 4, sanitize_excel_text(dept))
        ws.cell(r_idx, 5, sanitize_excel_text(mg))
        ws.cell(r_idx, 6, sanitize_excel_text(cp))
        ws.cell(r_idx, 7, sanitize_excel_text(label))
        _write_money(ws, r_idx, 8, entity.deltas["total_debt"].abs_delta)
        _write_pct(ws, r_idx, 9, entity.deltas["total_debt"].percent_delta)
        ws.cell(r_idx, 10, "да" if entity.overdue_profile_changed else "нет")
    last = max(len(interesting) + 1, 1)
    ws.auto_filter.ref = f"A1:J{last}"
    _apply_column_widths(ws, (22, 10, 28, 14, 18, 22, 28, 14, 12, 18))


def _fill_control(ws, comparison: CycleComparison) -> None:
    headers = (
        "тип",
        "имя",
        "ok",
        "diagnostic",
        "left",
        "right",
        "цикл",
        "match_key",
        "count",
        "raw_labels",
    )
    _write_header(ws, headers)
    r_idx = 2
    for check in comparison.control_equalities:
        ws.cell(r_idx, 1, "equality")
        ws.cell(r_idx, 2, check.name)
        ws.cell(r_idx, 3, "ok" if check.ok else "FAIL")
        ws.cell(r_idx, 4, "да" if check.diagnostic else "нет")
        _write_money(ws, r_idx, 5, check.left)
        _write_money(ws, r_idx, 6, check.right)
        r_idx += 1
    for collision in comparison.collisions:
        ws.cell(r_idx, 1, "collision")
        ws.cell(r_idx, 2, "match_key_collision")
        ws.cell(r_idx, 3, "FAIL")
        ws.cell(r_idx, 4, "нет")
        ws.cell(r_idx, 7, collision.cycle_side)
        ws.cell(r_idx, 8, collision.match_key)
        ws.cell(r_idx, 9, collision.count)
        ws.cell(
            r_idx,
            10,
            sanitize_excel_text(" | ".join(collision.raw_labels)),
        )
        r_idx += 1
    last = max(r_idx - 1, 1)
    ws.auto_filter.ref = f"A1:J{last}"
    _apply_column_widths(ws, (12, 48, 8, 12, 14, 14, 12, 28, 8, 40))


def build_core_workbook(comparison: CycleComparison) -> WorkbookType:
    wb = _prepare_workbook()
    by_id = _positions_by_id(comparison.entities)

    _fill_summary(wb[SHEET_SUMMARY], comparison)
    _fill_aggregate_sheet(
        wb[SHEET_DEPARTMENTS],
        build_department_rows(comparison),
        label_header="Отдел",
    )
    _fill_aggregate_sheet(
        wb[SHEET_MANAGER_GROUPS],
        build_manager_group_rows(comparison),
        label_header="ManagerGroup",
    )

    counterparties = [e for e in comparison.entities if e.outline_level == 1]
    # L3 lives as columns on contracts/documents; still surface L3-only matches
    # on Договоры for visibility (object column filled).
    contracts_and_objects = [
        e for e in comparison.entities if e.outline_level in (2, 3)
    ]
    documents = [e for e in comparison.entities if e.outline_level == 4]

    _fill_entity_sheet(
        wb[SHEET_COUNTERPARTIES],
        counterparties,
        by_id=by_id,
        include_object=False,
        include_contract=False,
    )
    _fill_entity_sheet(
        wb[SHEET_CONTRACTS],
        contracts_and_objects,
        by_id=by_id,
        include_object=True,
        include_contract=True,
    )
    _fill_entity_sheet(
        wb[SHEET_DOCUMENTS],
        documents,
        by_id=by_id,
        include_object=True,
        include_contract=True,
    )
    _fill_changes(wb[SHEET_CHANGES], comparison)
    _fill_control(wb[SHEET_CONTROL], comparison)
    return wb


def workbook_to_bytes(workbook: WorkbookType) -> bytes:
    bio = BytesIO()
    workbook.save(bio)
    return _normalize_xlsx_bytes(bio.getvalue())


def build_core_excel_bytes(comparison: CycleComparison) -> tuple[bytes, str]:
    """Return (xlsx_bytes, sha256_hex) for a CORE artifact."""
    wb = build_core_workbook(comparison)
    raw = workbook_to_bytes(wb)
    digest = hashlib.sha256(raw).hexdigest()
    return raw, digest
