"""Скрытые/визуально сжатые колонки не считаются отсутствующими (§4).

`regional_valid_hidden_columns.xls` строится с `hidden_columns=(5, 7)`
(«Отсрочка платежа», «Сумма документа») и `narrow_columns=(6,)` («Сумма
кредита») — воспроизводит `regional_2026-07-08` (§4).
"""
from __future__ import annotations

from pathlib import Path

from app.infrastructure.excel.fingerprint import SHEET_NAME
from app.infrastructure.excel.reader import read_workbook
from app.infrastructure.excel.validator import validate_confirmed_template_file


def test_hidden_and_narrow_columns_still_read(fixtures_dir: Path) -> None:
    path = fixtures_dir / "regional_valid_hidden_columns.xls"

    sheet = read_workbook(path, SHEET_NAME)
    assert sheet.column_hidden[5] is True
    assert sheet.column_hidden[7] is True
    assert sheet.column_width[6] is not None and sheet.column_width[6] < 100

    result = validate_confirmed_template_file(path)
    assert result.is_valid, result.rejection_reasons

    level1_rows = [r for r in result.parsed.debt_rows if r.outline_level == 1]
    assert any(r.credit_limit is not None for r in level1_rows)
    assert any(r.document_amount is not None for r in level1_rows)
