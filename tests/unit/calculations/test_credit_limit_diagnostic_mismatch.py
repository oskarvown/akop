"""Расхождение «Итого» по «Сумме кредита» — диагностика, не отказ (§6.2)."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.infrastructure.excel.validator import validate_confirmed_template_file


def test_credit_limit_mismatch_does_not_reject_file(fixtures_dir: Path) -> None:
    result = validate_confirmed_template_file(fixtures_dir / "regional_valid_credit_limit_mismatch.xls")

    assert result.is_valid is True
    assert result.rejection_reasons == ()
    assert result.reconciliation.credit_limit.within_tolerance is False
    assert result.reconciliation.credit_limit.difference == Decimal("50000.00")
    assert result.reconciliation.credit_limit.blocking is False
    assert result.reconciliation.is_blocking is False


def test_other_additive_metrics_still_blocking_on_mismatch(fixtures_dir: Path) -> None:
    """Только «Сумма кредита» имеет диагностическую политику — остальные метрики блокируют."""
    result = validate_confirmed_template_file(fixtures_dir / "regional_valid_basic.xls")
    for metric in result.reconciliation.additive:
        assert metric.blocking is True
