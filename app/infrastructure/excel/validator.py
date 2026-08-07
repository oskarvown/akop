"""Оркестрация валидации файла по единому подтверждённому шаблону (все 5 отделов)
— см. `docs/DATA_CONTRACT.md` §4, §4.1.

Валидатор не зависит от `Department`: он проверяет только физическую
структуру файла (17 колонок, заголовки, иерархию) и не принимает и не
использует `Department` — привязка файла к конкретному отделу выполняется
вызывающей стороной (Telegram-хендлер) до вызова persistence.

Скрытая/визуально узкая колонка **не** считается основанием для отклонения —
чтение всегда идёт по физической позиции (`app/infrastructure/excel/reader.py`),
свойства `hidden`/`width` не используются валидатором.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.domain.calculations.reconciliation import ReconciliationReport, reconcile
from app.infrastructure.excel.dto import ParsedSourceFile
from app.infrastructure.excel.fingerprint import (
    BUCKET_HEADER_ROW_LABELS,
    EXPECTED_COLUMN_COUNT,
    EXPECTED_UNIQUE_HEADER_LABELS,
    FINGERPRINT_NAME,
    MONEY_HEADER_ROW_LABELS,
    SHEET_NAME,
)
from app.infrastructure.excel.header_parser import (
    HeaderLocation,
    HeaderNotFoundError,
    find_header_location,
    parse_report_date,
)
from app.infrastructure.excel.reader import ExcelReadError, RawSheet, SheetNotFoundError, read_workbook
from app.infrastructure.excel.row_classifier import RowStructureError, classify_rows
from app.infrastructure.excel.value_parsing import parse_label


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    fingerprint_name: str
    parsed: ParsedSourceFile | None
    reconciliation: ReconciliationReport | None
    rejection_reasons: tuple[str, ...]
    row_diagnostics: tuple[str, ...]


def _invalid(reasons: list[str]) -> ValidationResult:
    return ValidationResult(
        is_valid=False,
        fingerprint_name=FINGERPRINT_NAME,
        parsed=None,
        reconciliation=None,
        rejection_reasons=tuple(reasons),
        row_diagnostics=(),
    )


def _diagnose_missing_headers(sheet: RawSheet) -> list[str]:
    """Best-effort список отсутствующих подписей, когда число колонок не 17 (§8)."""
    try:
        header = find_header_location(sheet)
    except HeaderNotFoundError:
        return []
    found: set[str] = set()
    for row in sheet.rows[header.hierarchy_labels_start_row : header.first_data_row]:
        for value in row.values:
            if isinstance(value, str) and value.strip():
                found.add(value.strip())
    missing = sorted(EXPECTED_UNIQUE_HEADER_LABELS - found)
    if not missing:
        return []
    return [f"Отсутствуют колонки (по заголовку): {', '.join(missing)}"]


def _check_header_positions(sheet: RawSheet, header: HeaderLocation) -> list[str]:
    errors: list[str] = []
    bucket_row = sheet.rows[header.hierarchy_labels_start_row]
    money_row = sheet.rows[header.hierarchy_labels_start_row + 1]
    for col, expected in BUCKET_HEADER_ROW_LABELS.items():
        actual = parse_label(bucket_row.values[col])
        if actual != expected:
            errors.append(
                f"Колонка {col} (строка-подпись корзин просрочки): ожидалось {expected!r}, "
                f"получено {actual!r}"
            )
    for col, expected in MONEY_HEADER_ROW_LABELS.items():
        actual = parse_label(money_row.values[col])
        if actual != expected:
            errors.append(
                f"Колонка {col} (строка-подпись денежных полей): ожидалось {expected!r}, "
                f"получено {actual!r}"
            )
    return errors


def validate_confirmed_template_file(path: Path) -> ValidationResult:
    """Полный пайплайн валидации одного файла по единому подтверждённому шаблону
    (применим к файлу любого из 5 отделов — функция не принимает `Department`).
    """
    try:
        sheet = read_workbook(path, SHEET_NAME)
    except SheetNotFoundError as exc:
        return _invalid([f"Лист не найден: {exc}"])
    except ExcelReadError as exc:
        return _invalid([f"Файл повреждён или нечитаем: {exc}"])

    if sheet.n_cols != EXPECTED_COLUMN_COUNT:
        reasons = [
            f"Ожидалось {EXPECTED_COLUMN_COUNT} физических колонок, найдено {sheet.n_cols}"
        ]
        reasons.extend(_diagnose_missing_headers(sheet))
        return _invalid(reasons)

    try:
        header = find_header_location(sheet)
    except HeaderNotFoundError as exc:
        return _invalid([f"Analytical/hierarchy header не сопоставляется с fingerprint: {exc}"])

    position_errors = _check_header_positions(sheet, header)
    if position_errors:
        return _invalid(position_errors)

    try:
        report_date = parse_report_date(sheet, header)
    except HeaderNotFoundError as exc:
        return _invalid([str(exc)])

    try:
        classification = classify_rows(sheet, header.first_data_row)
    except RowStructureError as exc:
        return _invalid([str(exc)])

    parsed = ParsedSourceFile(
        report_date=report_date,
        manager_groups=classification.manager_groups,
        debt_rows=classification.debt_rows,
        grand_total=classification.grand_total,
        row_diagnostics=classification.row_diagnostics,
    )

    reconciliation = reconcile(parsed.debt_rows, parsed.grand_total)
    if reconciliation.is_blocking:
        reasons = [
            "Расхождение 'Итого' по метрике "
            f"{m.metric!r}: сумма строк уровня 1 = {m.calculated_total}, "
            f"'Итого' файла = {m.reported_total}, разница = {m.difference}"
            for m in reconciliation.blocking_mismatches
        ]
        return ValidationResult(
            is_valid=False,
            fingerprint_name=FINGERPRINT_NAME,
            parsed=parsed,
            reconciliation=reconciliation,
            rejection_reasons=tuple(reasons),
            row_diagnostics=parsed.row_diagnostics,
        )

    return ValidationResult(
        is_valid=True,
        fingerprint_name=FINGERPRINT_NAME,
        parsed=parsed,
        reconciliation=reconciliation,
        rejection_reasons=(),
        row_diagnostics=parsed.row_diagnostics,
    )
