"""Классификация строк данных по outline level 0–4 — см. `docs/DATA_CONTRACT.md` §3.

Строки шапки/параметров (`Параметры:`, `Отбор:` и подписи аналитической шапки)
уже исключены позиционно самим `header_parser.find_header_location` (это то,
что до `first_data_row`) — здесь обрабатываются только реальные данные.

Уровни 2–4 (договор/объект/документ) сохраняются с явной родительской связью
(`parent_row_index`) и не отбрасываются — они нужны для детализации отчёта
(Stage 4) и не участвуют в reconciliation (`app/domain/calculations/reconciliation.py`
учитывает только `outline_level == 1`).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.infrastructure.excel.dto import GrandTotalRow, ParsedDebtRow, ParsedManagerGroupRow
from app.infrastructure.excel.fingerprint import (
    COL_ADVANCE,
    COL_COMMENT,
    COL_CREDIT_LIMIT,
    COL_DOCUMENT_AMOUNT,
    COL_HIERARCHY_LABEL,
    COL_NOT_DUE,
    COL_OVERDUE_1_7,
    COL_OVERDUE_8_14,
    COL_OVERDUE_15_21,
    COL_OVERDUE_22_30,
    COL_OVERDUE_OVER_31,
    COL_PAYMENT_DEFERRAL_DAYS,
    COL_TOTAL_DEBT,
)
from app.infrastructure.excel.reader import RawRow, RawSheet
from app.infrastructure.excel.value_parsing import (
    CellTypeError,
    parse_comment,
    parse_decimal,
    parse_label,
    parse_payment_deferral_days,
)

_GRAND_TOTAL_PREFIX = "итог"


class RowStructureError(Exception):
    """Критическая структурная ошибка иерархии строк (§4.1) — файл отклоняется."""


@dataclass(frozen=True)
class ClassificationResult:
    manager_groups: tuple[ParsedManagerGroupRow, ...]
    debt_rows: tuple[ParsedDebtRow, ...]
    grand_total: GrandTotalRow
    row_diagnostics: tuple[str, ...]


def _row_money_values(row: RawRow) -> dict[str, object]:
    return {
        "credit_limit": row.values[COL_CREDIT_LIMIT],
        "document_amount": row.values[COL_DOCUMENT_AMOUNT],
        "total_debt": row.values[COL_TOTAL_DEBT],
        "advance": row.values[COL_ADVANCE],
        "not_due": row.values[COL_NOT_DUE],
        "overdue_1_7": row.values[COL_OVERDUE_1_7],
        "overdue_8_14": row.values[COL_OVERDUE_8_14],
        "overdue_15_21": row.values[COL_OVERDUE_15_21],
        "overdue_22_30": row.values[COL_OVERDUE_22_30],
        "overdue_over_31": row.values[COL_OVERDUE_OVER_31],
    }


def _parse_money_fields(row: RawRow, row_label_for_errors: str) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for field_name, raw_value in _row_money_values(row).items():
        try:
            parsed[field_name] = parse_decimal(raw_value)
        except CellTypeError as exc:
            raise RowStructureError(
                f"Недопустимое значение в денежной колонке {field_name!r} "
                f"строки {row.index} ({row_label_for_errors!r}): {exc}"
            ) from exc
    return parsed


def classify_rows(sheet: RawSheet, first_data_row: int) -> ClassificationResult:
    if sheet.n_rows <= first_data_row:
        raise RowStructureError("В файле нет строк данных после шапки")

    last_row = sheet.rows[sheet.n_rows - 1]
    last_label = parse_label(last_row.values[COL_HIERARCHY_LABEL])
    if not last_label.strip().lower().startswith(_GRAND_TOTAL_PREFIX):
        raise RowStructureError(
            f"Последняя строка листа ({last_row.index}) не является строкой «Итого»: "
            f"{last_label!r}"
        )
    if last_row.outline_level != 0:
        raise RowStructureError(
            f"Строка «Итого» ({last_row.index}) имеет неожиданный outline_level="
            f"{last_row.outline_level} (ожидался 0)"
        )
    grand_total_money = _parse_money_fields(last_row, last_label)
    grand_total = GrandTotalRow(row_index=last_row.index, **grand_total_money)

    manager_groups: list[ParsedManagerGroupRow] = []
    debt_rows: list[ParsedDebtRow] = []
    diagnostics: list[str] = []

    current_manager_group_row: int | None = None
    current_counterparty_row: int | None = None
    parent_at_level: dict[int, int] = {}  # level -> row_index последнего виденного предка

    for row in sheet.rows[first_data_row : sheet.n_rows - 1]:
        level = row.outline_level
        label = parse_label(row.values[COL_HIERARCHY_LABEL])

        if level == 0:
            manager_groups.append(ParsedManagerGroupRow(row_index=row.index, raw_label=label))
            current_manager_group_row = row.index
            current_counterparty_row = None
            parent_at_level = {}
            continue

        if level not in (1, 2, 3, 4):
            raise RowStructureError(
                f"Недопустимый outline_level={level} в строке {row.index} ({label!r})"
            )
        if current_manager_group_row is None:
            raise RowStructureError(
                f"Строка уровня {level} ({row.index}, {label!r}) встречена до первой "
                "строки ManagerGroup (outline_level=0)"
            )
        if level > 1 and parent_at_level.get(level - 1) is None:
            raise RowStructureError(
                f"Строка уровня {level} ({row.index}, {label!r}) не имеет родителя "
                f"уровня {level - 1} — нарушена иерархия"
            )

        parent_row_index = parent_at_level.get(level - 1) if level > 1 else None
        if level == 1:
            current_counterparty_row = row.index
        assert current_counterparty_row is not None  # гарантировано проверкой уровня выше

        deferral = parse_payment_deferral_days(row.values[COL_PAYMENT_DEFERRAL_DAYS])
        if deferral.error is not None:
            diagnostics.append(f"row {row.index} ({label!r}): {deferral.error}")

        money = _parse_money_fields(row, label)

        debt_rows.append(
            ParsedDebtRow(
                row_index=row.index,
                outline_level=level,
                raw_label=label,
                manager_group_row_index=current_manager_group_row,
                parent_row_index=parent_row_index,
                counterparty_row_index=current_counterparty_row,
                payment_deferral_days=deferral.days,
                payment_deferral_error=deferral.error,
                comment_raw=parse_comment(row.values[COL_COMMENT]),
                **money,
            )
        )
        parent_at_level[level] = row.index
        # Более глубокие уровни, оставшиеся от предыдущей ветки, больше не актуальны.
        for deeper in range(level + 1, 5):
            parent_at_level.pop(deeper, None)

    if not manager_groups:
        raise RowStructureError("В файле не найдено ни одной строки ManagerGroup (outline_level=0)")

    return ClassificationResult(
        manager_groups=tuple(manager_groups),
        debt_rows=tuple(debt_rows),
        grand_total=grand_total,
        row_diagnostics=tuple(diagnostics),
    )
