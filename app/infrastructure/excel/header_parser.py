"""Разбор многострочной шапки отчёта — см. `docs/DATA_CONTRACT.md` §3.

Блок параметров (`Параметры:`/`Отбор:`) может занимать разное число строк
(список отобранных менеджеров может быть длиннее одной строки), поэтому конец
шапки ищется **по содержимому** колонки A (последовательность
`HIERARCHY_HEADER_LABELS`), а не по фиксированному номеру строки.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from app.infrastructure.excel.fingerprint import COL_HIERARCHY_LABEL, HIERARCHY_HEADER_LABELS
from app.infrastructure.excel.reader import RawSheet
from app.infrastructure.excel.value_parsing import parse_label

_REPORT_DATE_RE = re.compile(r"Дата отчета:\s*(\d{2})\.(\d{2})\.(\d{4})")

_MAX_HEADER_SEARCH_ROWS = 60


class HeaderNotFoundError(Exception):
    """Analytical/hierarchy header не сопоставляется с fingerprint (§4.1)."""


@dataclass(frozen=True)
class HeaderLocation:
    hierarchy_labels_start_row: int
    """Строка, содержащая первую подпись ("Менеджер")."""

    first_data_row: int
    """Первая строка данных (следующая после последней подписи шапки)."""


def find_header_location(sheet: RawSheet) -> HeaderLocation:
    labels = HIERARCHY_HEADER_LABELS
    n = len(labels)
    limit = min(sheet.n_rows, _MAX_HEADER_SEARCH_ROWS)
    for start in range(0, max(0, limit - n + 1)):
        window = tuple(
            parse_label(sheet.rows[start + offset].values[COL_HIERARCHY_LABEL])
            for offset in range(n)
        )
        if window == labels:
            return HeaderLocation(hierarchy_labels_start_row=start, first_data_row=start + n)
    raise HeaderNotFoundError(
        "Не найдена последовательность подписей иерархической шапки "
        f"{labels!r} в первых {limit} строках колонки A"
    )


def parse_report_date(sheet: RawSheet, header: HeaderLocation) -> dt.date:
    """Дата отчёта парсится из блока параметров (строки до шапки), не из даты загрузки."""
    for row in sheet.rows[: header.hierarchy_labels_start_row]:
        for value in row.values:
            if not isinstance(value, str):
                continue
            match = _REPORT_DATE_RE.search(value)
            if match:
                day, month, year = (int(part) for part in match.groups())
                return dt.date(year, month, day)
    raise HeaderNotFoundError("Не найдена строка 'Дата отчета: DD.MM.YYYY' в блоке параметров")
