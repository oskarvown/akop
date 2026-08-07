"""Fingerprint единого подтверждённого шаблона — см. `docs/DATA_CONTRACT.md` §3, §3.1, §10.

Подтверждён на 4 реальных файлах отдела «Региональный»
(`regional_2026-06-11/06-24/07-01/07-08`) и, по подтверждённому бизнес-решению
Александра, действует как единый входной контракт **для всех пяти
отделов** (СЗФО 1, СЗФО 2, Региональный, Москва, Фокин) — все они передают
файлы по одному и тому же утверждённому шаблону из 17 физических колонок.
Файл любого отдела с другой физической структурой отклоняется валидатором с
диагностикой (`docs/DATA_CONTRACT.md` §4.1) и должен рассматриваться как
отдельное изменение контракта, а не как повод создавать параллельный
fingerprint без фактических структурных различий.
"""
from __future__ import annotations

SHEET_NAME = "TDSheet"
EXPECTED_COLUMN_COUNT = 17

# Иерархические подписи колонки A (индекс 0) в порядке следования строк
# analytical/hierarchy header (`docs/DATA_CONTRACT.md` §3). Используются для
# позиционного поиска конца шапки/начала данных — не полагаемся на фиксированные
# номера строк, т.к. блок «Отбор:» может занимать разное число строк.
HIERARCHY_HEADER_LABELS: tuple[str, ...] = (
    "Менеджер",
    "Партнер",
    "Договор",
    "Объект расчетов",
    "Расчетный документ",
)

# Ожидаемые подписи по колонкам во второй ("Партнер") строке шапки — денежные
# заголовки (§3.1). Индекс 0 — колонка A (уже проверена через HIERARCHY_HEADER_LABELS).
MONEY_HEADER_ROW_LABELS: dict[int, str] = {
    5: "Отсрочка платежа",
    6: "Сумма кредита",
    7: "Сумма документа",
    8: "Долг",
    9: "Аванс",
    10: "Задолженность",
    11: "Задолженность",
    12: "Задолженность",
    13: "Задолженность",
    14: "Задолженность",
    15: "Задолженность",
}

# Ожидаемые подписи по колонкам в первой ("Менеджер") строке шапки — корзины
# просрочки (§3.1).
BUCKET_HEADER_ROW_LABELS: dict[int, str] = {
    10: "Не просрочено",
    11: "От 1 до 7 дней",
    12: "От 8 до 14 дней",
    13: "От 15 до 21 дней",
    14: "От 22 до 30 дней",
    15: "Свыше 31 дней",
}

# Позиции денежных/неденежных полей (0-индексация), см. §3.1.
COL_HIERARCHY_LABEL = 0
COL_PAYMENT_DEFERRAL_DAYS = 5
COL_CREDIT_LIMIT = 6
COL_DOCUMENT_AMOUNT = 7
COL_TOTAL_DEBT = 8
COL_ADVANCE = 9
COL_NOT_DUE = 10
COL_OVERDUE_1_7 = 11
COL_OVERDUE_8_14 = 12
COL_OVERDUE_15_21 = 13
COL_OVERDUE_22_30 = 14
COL_OVERDUE_OVER_31 = 15
COL_COMMENT = 16

# Обязательные денежные колонки (Decimal) — недопустимый тип => отклонение
# файла целиком (`docs/DATA_CONTRACT.md` §4.1), в отличие от
# `COL_PAYMENT_DEFERRAL_DAYS`, ошибка которой блокирует только запись (§6.1).
REQUIRED_DECIMAL_COLUMNS: tuple[int, ...] = (
    COL_CREDIT_LIMIT,
    COL_DOCUMENT_AMOUNT,
    COL_TOTAL_DEBT,
    COL_ADVANCE,
    COL_NOT_DUE,
    COL_OVERDUE_1_7,
    COL_OVERDUE_8_14,
    COL_OVERDUE_15_21,
    COL_OVERDUE_22_30,
    COL_OVERDUE_OVER_31,
)

# Метрики, участвующие в per-metric reconciliation с «Итого» (§6):
# аддитивные (блокирующие при расхождении) — все, кроме credit_limit.
ADDITIVE_RECONCILIATION_COLUMNS: tuple[int, ...] = (
    COL_DOCUMENT_AMOUNT,
    COL_TOTAL_DEBT,
    COL_ADVANCE,
    COL_NOT_DUE,
    COL_OVERDUE_1_7,
    COL_OVERDUE_8_14,
    COL_OVERDUE_15_21,
    COL_OVERDUE_22_30,
    COL_OVERDUE_OVER_31,
)

# «Сумма кредита» — диагностика, не блокирует файл (§6.2).
CREDIT_LIMIT_DIAGNOSTIC_COLUMN = COL_CREDIT_LIMIT

FINGERPRINT_NAME = "confirmed_template_v1"

# Объединённое множество уникальных подписей денежной/бакет шапки — используется
# для best-effort диагностики отсутствующих колонок, когда физическое число
# колонок уже не совпадает с fingerprint (§4.1, §8: пример
# `invalid_2026-07-15_missing_columns` — отсутствуют «Отсрочка платежа»,
# «Сумма кредита», «Сумма документа», «Не просрочено», корзина «От 15 до 21 дня»).
EXPECTED_UNIQUE_HEADER_LABELS: frozenset[str] = frozenset(
    set(MONEY_HEADER_ROW_LABELS.values()) | set(BUCKET_HEADER_ROW_LABELS.values())
)
