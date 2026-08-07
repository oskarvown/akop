"""Reconciliation строки «Итого» — по метрикам, не блоком. См. `docs/DATA_CONTRACT.md` §6.

Правила:

- сверка выполняется **только** по строкам уровня 1 (контрагент) — уровни 2–4
  исключены из суммирования, чтобы не считать одну и ту же сумму дважды (§3, §9);
- 9 аддитивных денежных метрик должны совпадать с «Итого» файла (с допуском на
  округление) — расхождение блокирует файл;
- «Сумма кредита» имеет отдельную политику: расхождение — диагностика
  (info/warning), не ошибка (§6.2);
- «Отсрочка платежа» не аддитивна и здесь не участвует (проверяется на уровне
  записи — см. `app/infrastructure/excel/value_parsing.parse_payment_deferral_days`).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.infrastructure.excel.dto import GrandTotalRow, ParsedDebtRow

ADDITIVE_METRICS: tuple[str, ...] = (
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

CREDIT_LIMIT_METRIC = "credit_limit"

DEFAULT_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class MetricReconciliation:
    metric: str
    reported_total: Decimal | None
    calculated_total: Decimal
    difference: Decimal
    blocking: bool
    within_tolerance: bool


@dataclass(frozen=True)
class ReconciliationReport:
    additive: tuple[MetricReconciliation, ...]
    credit_limit: MetricReconciliation

    @property
    def blocking_mismatches(self) -> tuple[MetricReconciliation, ...]:
        return tuple(m for m in self.additive if not m.within_tolerance)

    @property
    def is_blocking(self) -> bool:
        return len(self.blocking_mismatches) > 0

    def as_dict(self) -> dict[str, object]:
        def _metric_dict(m: MetricReconciliation) -> dict[str, object]:
            return {
                "metric": m.metric,
                "reported_total": str(m.reported_total) if m.reported_total is not None else None,
                "calculated_total": str(m.calculated_total),
                "difference": str(m.difference),
                "blocking": m.blocking,
                "within_tolerance": m.within_tolerance,
            }

        return {
            "additive": [_metric_dict(m) for m in self.additive],
            "credit_limit": _metric_dict(self.credit_limit),
        }


def _sum_level_1(rows: tuple[ParsedDebtRow, ...], field_name: str) -> Decimal:
    """Сумма по строкам **только** уровня 1 (контрагент) — без учёта 2–4 (§3, §9)."""
    total = Decimal("0")
    for row in rows:
        if row.outline_level != 1:
            continue
        value = getattr(row, field_name)
        if value is not None:
            total += value
    return total


def reconcile(
    debt_rows: tuple[ParsedDebtRow, ...],
    grand_total: GrandTotalRow,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> ReconciliationReport:
    additive: list[MetricReconciliation] = []
    for metric in ADDITIVE_METRICS:
        calculated = _sum_level_1(debt_rows, metric)
        reported = getattr(grand_total, metric)
        reported_for_diff = reported if reported is not None else Decimal("0")
        difference = calculated - reported_for_diff
        within = abs(difference) <= tolerance
        additive.append(
            MetricReconciliation(
                metric=metric,
                reported_total=reported,
                calculated_total=calculated,
                difference=difference,
                blocking=True,
                within_tolerance=within,
            )
        )

    calculated_credit_limit = _sum_level_1(debt_rows, CREDIT_LIMIT_METRIC)
    reported_credit_limit = grand_total.credit_limit
    reported_credit_limit_for_diff = (
        reported_credit_limit if reported_credit_limit is not None else Decimal("0")
    )
    credit_diff = calculated_credit_limit - reported_credit_limit_for_diff
    credit_limit_metric = MetricReconciliation(
        metric=CREDIT_LIMIT_METRIC,
        reported_total=reported_credit_limit,
        calculated_total=calculated_credit_limit,
        difference=credit_diff,
        blocking=False,
        within_tolerance=abs(credit_diff) <= tolerance,
    )

    return ReconciliationReport(additive=tuple(additive), credit_limit=credit_limit_metric)
