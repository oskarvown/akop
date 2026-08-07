"""Excel-парсер и валидация по единому подтверждённому шаблону всех 5 отделов
(Stage 2): СЗФО 1, СЗФО 2, Региональный, Москва, Фокин.

Пакет намеренно не делает eager re-export публичных имён из подмодулей
(`validator.py`, `persistence.py`, `checksum.py`, ...): `reconciliation.py`
импортирует `app.infrastructure.excel.dto`, а импорт любого подмодуля пакета
сначала выполняет `__init__.py` пакета — eager-импорт `persistence`/`validator`
здесь создавал циклический импорт (`validator` → `reconciliation` →
`app.infrastructure.excel.dto` → этот `__init__.py` → `persistence` →
`validator`, ещё не до конца инициализированный). Используйте прямые импорты
из подмодулей, например `from app.infrastructure.excel.validator import
validate_confirmed_template_file`.
"""
