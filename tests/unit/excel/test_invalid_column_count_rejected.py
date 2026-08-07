"""Негативный fixture (аналог `invalid_2026-07-15_missing_columns`) — см. `docs/DATA_CONTRACT.md` §8."""
from __future__ import annotations

from pathlib import Path

from app.infrastructure.excel.validator import validate_confirmed_template_file


def test_invalid_file_with_12_columns_is_rejected(fixtures_dir: Path) -> None:
    result = validate_confirmed_template_file(fixtures_dir / "regional_invalid_missing_columns.xlsx")

    assert result.is_valid is False
    assert result.parsed is None
    joined = " | ".join(result.rejection_reasons)
    assert "17" in joined
    # После обрезки пустого хвоста openpyxl-«физическая» 12-я колонка без
    # данных может не считаться — важно, что колонок меньше 17 и названы
    # отсутствующие обязательные заголовки (§8).
    assert any(token in joined for token in ("11", "12"))
    for expected_missing in (
        "Отсрочка платежа",
        "Сумма кредита",
        "Сумма документа",
        "Не просрочено",
        "От 15 до 21 дней",
    ):
        assert expected_missing in joined
