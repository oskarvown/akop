"""Reconciliation «Итого» — по метрикам, не блоком. См. `docs/DATA_CONTRACT.md` §6.

Прямая проверка на уровне `app.domain.calculations.reconciliation.reconcile`
(без сборки Excel-файла): расхождение по любой из 9 аддитивных метрик
(«Сумма документа», «Долг», «Аванс», «Не просрочено», 5 корзин просрочки)
блокирует файл; расхождение **только** по «Сумме кредита» — не блокирует
(§6.2). Соответствует
`docs/REQUIREMENTS_TRACEABILITY.md`
(`tests/unit/calculations/test_totals_reconciliation_per_metric.py`).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.calculations.reconciliation import ADDITIVE_METRICS, CREDIT_LIMIT_METRIC, reconcile
from app.infrastructure.excel.dto import GrandTotalRow, ParsedDebtRow

BASELINE_VALUE = Decimal("100.00")
MISMATCH_OFFSET = Decimal("50.00")  # заведомо больше допуска (0.01)

# Ровно 9 аддитивных метрик — контроль состава списка, а не только его наличия.
EXPECTED_ADDITIVE_METRICS = (
    "document_amount",
    "total_debt",
    "advance",
    "not_due",
    "overdue_1_7",
    "overdue_8_14",
    "overdue_15_21",
    "overdue_22_30",
    "overdue_over_31",
)


def test_exactly_nine_additive_metrics_defined() -> None:
    assert len(ADDITIVE_METRICS) == 9
    assert set(ADDITIVE_METRICS) == set(EXPECTED_ADDITIVE_METRICS)
    assert CREDIT_LIMIT_METRIC == "credit_limit"
    assert CREDIT_LIMIT_METRIC not in ADDITIVE_METRICS
    assert "payment_deferral_days" not in ADDITIVE_METRICS


def _level1_row(**overrides: object) -> ParsedDebtRow:
    fields: dict[str, object] = {
        "row_index": 10,
        "outline_level": 1,
        "raw_label": "Синтетический контрагент",
        "manager_group_row_index": 0,
        "parent_row_index": None,
        "counterparty_row_index": 10,
        "payment_deferral_days": None,
        "payment_deferral_error": None,
        "credit_limit": BASELINE_VALUE,
        "document_amount": BASELINE_VALUE,
        "total_debt": BASELINE_VALUE,
        "advance": BASELINE_VALUE,
        "not_due": BASELINE_VALUE,
        "overdue_1_7": BASELINE_VALUE,
        "overdue_8_14": BASELINE_VALUE,
        "overdue_15_21": BASELINE_VALUE,
        "overdue_22_30": BASELINE_VALUE,
        "overdue_over_31": BASELINE_VALUE,
        "comment_raw": None,
    }
    fields.update(overrides)
    return ParsedDebtRow(**fields)  # type: ignore[arg-type]


def _matching_grand_total(**overrides: object) -> GrandTotalRow:
    fields: dict[str, object] = {
        "row_index": 11,
        "credit_limit": BASELINE_VALUE,
        "document_amount": BASELINE_VALUE,
        "total_debt": BASELINE_VALUE,
        "advance": BASELINE_VALUE,
        "not_due": BASELINE_VALUE,
        "overdue_1_7": BASELINE_VALUE,
        "overdue_8_14": BASELINE_VALUE,
        "overdue_15_21": BASELINE_VALUE,
        "overdue_22_30": BASELINE_VALUE,
        "overdue_over_31": BASELINE_VALUE,
    }
    fields.update(overrides)
    return GrandTotalRow(**fields)  # type: ignore[arg-type]


def test_perfectly_matching_totals_are_not_blocking() -> None:
    report = reconcile((_level1_row(),), _matching_grand_total())
    assert report.is_blocking is False
    assert report.blocking_mismatches == ()
    assert report.credit_limit.within_tolerance is True


@pytest.mark.parametrize("metric", EXPECTED_ADDITIVE_METRICS)
def test_mismatch_in_each_additive_metric_blocks_file(metric: str) -> None:
    grand_total = _matching_grand_total(**{metric: BASELINE_VALUE + MISMATCH_OFFSET})

    report = reconcile((_level1_row(),), grand_total)

    assert report.is_blocking is True
    blocking_names = {m.metric for m in report.blocking_mismatches}
    assert blocking_names == {metric}
    # Расхождение ровно в одной метрике не должно "зацепить" остальные восемь.
    other_metrics = [m for m in report.additive if m.metric != metric]
    assert all(m.within_tolerance for m in other_metrics)


def test_mismatch_only_in_credit_limit_does_not_block_file() -> None:
    grand_total = _matching_grand_total(credit_limit=BASELINE_VALUE + MISMATCH_OFFSET)

    report = reconcile((_level1_row(),), grand_total)

    assert report.is_blocking is False
    assert report.blocking_mismatches == ()
    assert report.credit_limit.within_tolerance is False
    assert report.credit_limit.difference == -MISMATCH_OFFSET
    assert report.credit_limit.blocking is False
