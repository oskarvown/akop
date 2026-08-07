"""Промежуточные (не ORM) структуры результата парсинга одного Excel-файла.

Полностью независимы от SQLAlchemy — `app/infrastructure/excel/persistence.py`
превращает их в доменные модели `app/domain/models/*`. Раздельные слои дают
возможность гонять парсер/валидатор/reconciliation в unit-тестах без БД.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class ParsedManagerGroupRow:
    row_index: int
    raw_label: str


@dataclass(frozen=True)
class ParsedDebtRow:
    row_index: int
    outline_level: int  # 1..4
    raw_label: str
    manager_group_row_index: int
    parent_row_index: int | None  # None только для outline_level == 1
    counterparty_row_index: int
    """`row_index` строки-предка уровня 1 (контрагент); для самой строки уровня 1 — её собственный `row_index`."""

    payment_deferral_days: int | None
    payment_deferral_error: str | None

    credit_limit: Decimal | None
    document_amount: Decimal | None
    total_debt: Decimal | None
    advance: Decimal | None
    not_due: Decimal | None
    overdue_1_7: Decimal | None
    overdue_8_14: Decimal | None
    overdue_15_21: Decimal | None
    overdue_22_30: Decimal | None
    overdue_over_31: Decimal | None

    comment_raw: str | None


@dataclass(frozen=True)
class GrandTotalRow:
    row_index: int
    credit_limit: Decimal | None
    document_amount: Decimal | None
    total_debt: Decimal | None
    advance: Decimal | None
    not_due: Decimal | None
    overdue_1_7: Decimal | None
    overdue_8_14: Decimal | None
    overdue_15_21: Decimal | None
    overdue_22_30: Decimal | None
    overdue_over_31: Decimal | None


@dataclass(frozen=True)
class ParsedSourceFile:
    report_date: dt.date
    manager_groups: tuple[ParsedManagerGroupRow, ...]
    debt_rows: tuple[ParsedDebtRow, ...]
    grand_total: GrandTotalRow
    # Диагностика уровня записи (например, недопустимая отсрочка платежа),
    # не блокирующая файл — см. `docs/DATA_CONTRACT.md` §6.1.
    row_diagnostics: tuple[str, ...] = field(default_factory=tuple)
