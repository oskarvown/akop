"""«Отсрочка платежа» — неаддитивная метрика, ошибка уровня записи, не файла (§6.1)."""
from __future__ import annotations

from pathlib import Path

from app.domain.calculations.reconciliation import ADDITIVE_METRICS
from app.infrastructure.excel.validator import validate_confirmed_template_file


def test_payment_deferral_excluded_from_reconciliation_metrics() -> None:
    assert "payment_deferral_days" not in ADDITIVE_METRICS


def test_invalid_payment_deferral_is_row_diagnostic_not_file_error(fixtures_dir: Path) -> None:
    """`regional_valid_hidden_columns.xls` содержит дробную отсрочку (15.5) у одной записи."""
    result = validate_confirmed_template_file(fixtures_dir / "regional_valid_hidden_columns.xls")

    assert result.is_valid is True, result.rejection_reasons
    assert any("Дробное значение отсрочки" in diag for diag in result.row_diagnostics)

    affected = [r for r in result.parsed.debt_rows if r.payment_deferral_error is not None]
    assert len(affected) == 1
    assert affected[0].payment_deferral_days is None
    assert affected[0].outline_level == 1
