"""Outline levels 0–4 распознаются, уровни 2–4 сохраняются с родительскими связями.

См. `docs/DATA_CONTRACT.md` §3; `docs/REQUIREMENTS_TRACEABILITY.md`
(`tests/unit/excel/test_outline_levels.py`).
"""
from __future__ import annotations

from pathlib import Path

from app.infrastructure.excel.validator import validate_confirmed_template_file


def test_all_outline_levels_present_and_preserved(fixtures_dir: Path) -> None:
    result = validate_confirmed_template_file(fixtures_dir / "regional_valid_basic.xls")
    assert result.is_valid, result.rejection_reasons
    parsed = result.parsed

    levels_present = {row.outline_level for row in parsed.debt_rows}
    assert levels_present == {1, 2, 3, 4}
    assert len(parsed.manager_groups) == 2  # outline_level == 0


def test_parent_links_form_a_consistent_tree(fixtures_dir: Path) -> None:
    result = validate_confirmed_template_file(fixtures_dir / "regional_valid_basic.xls")
    parsed = result.parsed
    by_row_index = {row.row_index: row for row in parsed.debt_rows}

    level1_rows = [r for r in parsed.debt_rows if r.outline_level == 1]
    assert level1_rows
    for row in level1_rows:
        assert row.parent_row_index is None
        assert row.counterparty_row_index == row.row_index

    for level in (2, 3, 4):
        rows_at_level = [r for r in parsed.debt_rows if r.outline_level == level]
        assert rows_at_level, f"Ожидались строки уровня {level}"
        for row in rows_at_level:
            assert row.parent_row_index is not None
            parent = by_row_index.get(row.parent_row_index)
            if level == 2:
                # родитель уровня 2 — строка уровня 1 (не входит в debt_rows.parent
                # chain как DebtPosition — parent — это сама counterparty-строка).
                assert parent is not None
                assert parent.outline_level == 1
            else:
                assert parent is not None
                assert parent.outline_level == level - 1
            # У любой вложенной строки counterparty_row_index указывает на её
            # предка уровня 1 — это то, что используется вместо повторного
            # прохода по родительской цепочке при persistence.
            counterparty_row = by_row_index.get(row.counterparty_row_index)
            assert counterparty_row is not None
            assert counterparty_row.outline_level == 1


def test_manager_group_names_not_validated_as_person_names(fixtures_dir: Path) -> None:
    """`ManagerGroup.raw_name` непрозрачен — не фильтруется по шаблону ФИО (§2.2)."""
    result = validate_confirmed_template_file(fixtures_dir / "regional_valid_basic.xls")
    labels = {mg.raw_label for mg in result.parsed.manager_groups}
    assert "Тестовая Группа А" in labels
    assert "Тестовая Группа Б" in labels
