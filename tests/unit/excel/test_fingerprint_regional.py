"""17-колоночный fingerprint отдела «Региональный» на 4 обезличенных валидных файлах.

См. `docs/DATA_CONTRACT.md` §3, §3.1; реестр `docs/REQUIREMENTS_TRACEABILITY.md`
(`tests/unit/excel/test_fingerprint_regional.py`).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from app.infrastructure.excel.validator import validate_confirmed_template_file

VALID_FIXTURES = [
    ("regional_valid_basic.xls", dt.date(2026, 6, 11)),
    ("regional_valid_basic.xlsx", dt.date(2026, 6, 11)),
    ("regional_valid_credit_limit_mismatch.xls", dt.date(2026, 7, 1)),
    ("regional_valid_hidden_columns.xls", dt.date(2026, 7, 8)),
]


@pytest.mark.parametrize("filename,expected_date", VALID_FIXTURES)
def test_valid_regional_files_pass_fingerprint(
    fixtures_dir: Path, filename: str, expected_date: dt.date
) -> None:
    result = validate_confirmed_template_file(fixtures_dir / filename)

    assert result.is_valid, result.rejection_reasons
    assert result.fingerprint_name == "confirmed_template_v1"
    assert result.parsed is not None
    assert result.parsed.report_date == expected_date


def test_both_xls_and_xlsx_formats_supported(fixtures_dir: Path) -> None:
    """Один и тот же контракт данных читается и из .xls (xlrd), и из .xlsx (openpyxl)."""
    xls_result = validate_confirmed_template_file(fixtures_dir / "regional_valid_basic.xls")
    xlsx_result = validate_confirmed_template_file(fixtures_dir / "regional_valid_basic.xlsx")

    assert xls_result.is_valid
    assert xlsx_result.is_valid
    assert len(xls_result.parsed.debt_rows) == len(xlsx_result.parsed.debt_rows)
    assert len(xls_result.parsed.manager_groups) == len(xlsx_result.parsed.manager_groups)


def test_decimal_used_for_money_columns(fixtures_dir: Path) -> None:
    result = validate_confirmed_template_file(fixtures_dir / "regional_valid_basic.xls")
    assert result.is_valid
    level1_rows = [r for r in result.parsed.debt_rows if r.outline_level == 1]
    assert level1_rows
    for row in level1_rows:
        assert isinstance(row.credit_limit, Decimal)
        assert isinstance(row.total_debt, Decimal)


def test_payment_deferral_days_is_int_or_none(fixtures_dir: Path) -> None:
    result = validate_confirmed_template_file(fixtures_dir / "regional_valid_basic.xls")
    assert result.is_valid
    level1_rows = [r for r in result.parsed.debt_rows if r.outline_level == 1]
    for row in level1_rows:
        assert row.payment_deferral_days is None or isinstance(row.payment_deferral_days, int)
