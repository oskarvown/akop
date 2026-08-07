"""Генерация статических обезличенных fixture-файлов Stage 2 в `tests/fixtures/regional/`.

Запуск: `python -m tests.fixtures.generate_regional_fixtures` из корня репозитория
(после `pip install -r requirements-dev.txt`, включает `xlwt`). Файлы коммитятся
в репозиторий как обычные тестовые данные — в отличие от `private_inputs/`, они
не содержат ничего, связанного с реальными файлами Александра.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from tests.fixtures.regional_builder import (
    FIXTURES_DIR,
    ContractSpec,
    CounterpartySpec,
    DocumentSpec,
    ManagerGroupSpec,
    Money,
    ObjectSpec,
    WorkbookSpec,
    build_invalid_missing_columns_xlsx,
    build_regional_xls,
    build_regional_xlsx,
)


def _basic_spec(report_date: str) -> WorkbookSpec:
    manager_groups = (
        ManagerGroupSpec(
            label="Тестовая Группа А",
            counterparties=(
                CounterpartySpec(
                    label="Клиент Один",
                    money=Money(
                        payment_deferral_days=30,
                        credit_limit=Decimal("100000"),
                        document_amount=Decimal("50000"),
                        total_debt=Decimal("40000"),
                        advance=Decimal("5000"),
                        not_due=Decimal("30000"),
                        overdue_1_7=Decimal("5000"),
                        overdue_8_14=Decimal("3000"),
                        overdue_15_21=Decimal("1000"),
                        overdue_22_30=Decimal("500"),
                        overdue_over_31=Decimal("500"),
                        comment="тестовый комментарий",
                    ),
                    contracts=(
                        ContractSpec(
                            label="Договор 1",
                            money=Money(total_debt=Decimal("25000")),
                            objects=(
                                ObjectSpec(
                                    label="Объект 1",
                                    money=Money(total_debt=Decimal("25000")),
                                    documents=(
                                        DocumentSpec(
                                            label="Документ 1",
                                            money=Money(total_debt=Decimal("25000")),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                        ContractSpec(
                            label="Договор 2",
                            money=Money(total_debt=Decimal("15000")),
                            objects=(
                                ObjectSpec(
                                    label="Объект 2",
                                    money=Money(total_debt=Decimal("15000")),
                                    documents=(
                                        DocumentSpec(
                                            label="Документ 2",
                                            money=Money(total_debt=Decimal("15000")),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
                CounterpartySpec(
                    label="Клиент Два",
                    money=Money(
                        payment_deferral_days=None,
                        credit_limit=Decimal("50000"),
                        document_amount=Decimal("20000"),
                        total_debt=Decimal("15000"),
                        advance=Decimal("2000"),
                        not_due=Decimal("10000"),
                        overdue_1_7=Decimal("2000"),
                        overdue_8_14=Decimal("1000"),
                        overdue_15_21=Decimal("1000"),
                        overdue_22_30=Decimal("500"),
                        overdue_over_31=Decimal("500"),
                    ),
                    contracts=(
                        ContractSpec(
                            label="Договор 3",
                            money=Money(total_debt=Decimal("15000")),
                            objects=(
                                ObjectSpec(
                                    label="Объект 3",
                                    money=Money(total_debt=Decimal("15000")),
                                    documents=(
                                        DocumentSpec(
                                            label="Документ 3",
                                            money=Money(total_debt=Decimal("15000")),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        ManagerGroupSpec(
            label="Тестовая Группа Б",
            counterparties=(
                CounterpartySpec(
                    label="Клиент Три",
                    money=Money(
                        payment_deferral_days=14,
                        credit_limit=Decimal("70000"),
                        document_amount=Decimal("30000"),
                        total_debt=Decimal("20000"),
                        advance=Decimal("1000"),
                        not_due=Decimal("15000"),
                        overdue_1_7=Decimal("1000"),
                        overdue_8_14=Decimal("1000"),
                        overdue_15_21=Decimal("1000"),
                        overdue_22_30=Decimal("1000"),
                        overdue_over_31=Decimal("1000"),
                    ),
                    contracts=(
                        ContractSpec(
                            label="Договор 4",
                            money=Money(total_debt=Decimal("20000")),
                            objects=(
                                ObjectSpec(
                                    label="Объект 4",
                                    money=Money(total_debt=Decimal("20000")),
                                    documents=(
                                        DocumentSpec(
                                            label="Документ 4",
                                            money=Money(total_debt=Decimal("20000")),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )

    grand_total = Money(
        credit_limit=Decimal("220000"),
        document_amount=Decimal("100000"),
        total_debt=Decimal("75000"),
        advance=Decimal("8000"),
        not_due=Decimal("55000"),
        overdue_1_7=Decimal("8000"),
        overdue_8_14=Decimal("5000"),
        overdue_15_21=Decimal("3000"),
        overdue_22_30=Decimal("2000"),
        overdue_over_31=Decimal("2000"),
    )

    return WorkbookSpec(report_date=report_date, manager_groups=manager_groups, grand_total=grand_total)


def generate_all(output_dir: Path = FIXTURES_DIR) -> None:
    basic = _basic_spec("11.06.2026")
    build_regional_xls(basic, output_dir / "regional_valid_basic.xls")
    build_regional_xlsx(basic, output_dir / "regional_valid_basic.xlsx")

    mismatch_spec = _basic_spec("01.07.2026")
    mismatch_spec = WorkbookSpec(
        report_date=mismatch_spec.report_date,
        manager_groups=mismatch_spec.manager_groups,
        grand_total=Money(
            credit_limit=Decimal("170000"),  # намеренное расхождение (§6.2) — диагностика, не отказ
            document_amount=mismatch_spec.grand_total.document_amount,
            total_debt=mismatch_spec.grand_total.total_debt,
            advance=mismatch_spec.grand_total.advance,
            not_due=mismatch_spec.grand_total.not_due,
            overdue_1_7=mismatch_spec.grand_total.overdue_1_7,
            overdue_8_14=mismatch_spec.grand_total.overdue_8_14,
            overdue_15_21=mismatch_spec.grand_total.overdue_15_21,
            overdue_22_30=mismatch_spec.grand_total.overdue_22_30,
            overdue_over_31=mismatch_spec.grand_total.overdue_over_31,
        ),
    )
    build_regional_xls(mismatch_spec, output_dir / "regional_valid_credit_limit_mismatch.xls")

    hidden_spec_base = _basic_spec("08.07.2026")
    # Один контрагент получает недопустимое (дробное) значение отсрочки платежа —
    # диагностика уровня записи, не блокирует файл (§6.1).
    patched_groups = list(hidden_spec_base.manager_groups)
    group_a = patched_groups[0]
    counterparties = list(group_a.counterparties)
    client_two = counterparties[1]
    counterparties[1] = CounterpartySpec(
        label=client_two.label,
        money=Money(**{**client_two.money.__dict__, "payment_deferral_days": 15.5}),
        contracts=client_two.contracts,
    )
    patched_groups[0] = ManagerGroupSpec(label=group_a.label, counterparties=tuple(counterparties))
    hidden_spec = WorkbookSpec(
        report_date=hidden_spec_base.report_date,
        manager_groups=tuple(patched_groups),
        grand_total=hidden_spec_base.grand_total,
        hidden_columns=(5, 7),
        narrow_columns=(6,),
    )
    build_regional_xls(hidden_spec, output_dir / "regional_valid_hidden_columns.xls")

    build_invalid_missing_columns_xlsx(
        "15.07.2026", output_dir / "regional_invalid_missing_columns.xlsx"
    )


if __name__ == "__main__":
    generate_all()
    print(f"Fixtures written to {FIXTURES_DIR}")
