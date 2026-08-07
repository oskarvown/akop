"""Канонический состав отделов — см. `docs/DATA_CONTRACT.md` §2, §2.4.

Регрессионный тест на состав `Department`: фиксирует ожидаемые 5 значений
(включая добавленный отдел «Фокин»), чтобы случайное удаление/переименование
значения enum ловилось на уровне unit-теста, а не при первом реальном
использовании валидатора/persistence.
"""
from __future__ import annotations

from app.domain.enums import Department

EXPECTED_DEPARTMENTS: frozenset[str] = frozenset(
    {"szfo_1", "szfo_2", "regional", "moscow", "fokin"}
)


def test_department_enum_has_exactly_five_canonical_values() -> None:
    values = {member.value for member in Department}
    assert values == EXPECTED_DEPARTMENTS
    assert len(Department) == 5


def test_department_fokin_is_present() -> None:
    assert Department.FOKIN.value == "fokin"
    assert Department.FOKIN in list(Department)
