"""Уровни 2–4 исключены из reconciliation агрегатов — нет двойного суммирования.

См. `docs/DATA_CONTRACT.md` §3, §9; `docs/REQUIREMENTS_TRACEABILITY.md`
(`tests/unit/calculations/test_no_double_counting_nested_rows.py`).

`regional_valid_basic.xls` сконструирован так, что сумма `total_debt` по
строкам уровня 1 (75000) точно совпадает с «Итого» файла, а если бы по ошибке
суммировались ещё и вложенные уровни 2–4 (которые зеркалируют те же самые
суммы как раскрытие/детализацию), получившаяся сумма была бы кратно больше и
не совпадала бы с «Итого». Это делает тест чувствительным к регрессии
двойного счёта.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.domain.calculations.reconciliation import reconcile
from app.infrastructure.excel.validator import validate_confirmed_template_file


def test_reconciliation_sums_only_level_1_rows(fixtures_dir: Path) -> None:
    result = validate_confirmed_template_file(fixtures_dir / "regional_valid_basic.xls")
    assert result.is_valid, result.rejection_reasons
    parsed = result.parsed

    level1_total_debt = sum(
        (r.total_debt for r in parsed.debt_rows if r.outline_level == 1), Decimal("0")
    )
    all_levels_total_debt = sum(
        (r.total_debt for r in parsed.debt_rows if r.total_debt is not None), Decimal("0")
    )

    # Уровни 2-4 в этом fixture зеркалируют total_debt контрагента, поэтому
    # наивная сумма по всем уровням заведомо больше суммы только по уровню 1.
    assert all_levels_total_debt > level1_total_debt
    assert level1_total_debt == parsed.grand_total.total_debt

    report = reconcile(parsed.debt_rows, parsed.grand_total)
    total_debt_metric = next(m for m in report.additive if m.metric == "total_debt")
    assert total_debt_metric.calculated_total == level1_total_debt
    assert total_debt_metric.within_tolerance is True


def test_reconciliation_would_fail_if_nested_levels_were_included(fixtures_dir: Path) -> None:
    """Регрессионная проверка: намеренно засчитанные уровни 2-4 ломают сверку."""
    result = validate_confirmed_template_file(fixtures_dir / "regional_valid_basic.xls")
    parsed = result.parsed

    naive_sum = sum(
        (r.total_debt for r in parsed.debt_rows if r.total_debt is not None), Decimal("0")
    )
    assert naive_sum != parsed.grand_total.total_debt, (
        "Fixture должен демонстрировать расхождение при наивном суммировании всех уровней"
    )
