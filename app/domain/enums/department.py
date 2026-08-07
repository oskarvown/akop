"""Отделы — см. `docs/DATA_CONTRACT.md` §2.

По бизнес-решению Александра единый 17-колоночный fingerprint (Stage 2)
действует для всех 5 отделов — см. `app/infrastructure/excel/fingerprint.py`.
`FOKIN` — отдельный отдел из одного менеджера, добавлен наравне с остальными
четырьмя (актуальный список см. `docs/DATA_CONTRACT.md` §2, §2.4).
"""
from __future__ import annotations

import enum


class Department(str, enum.Enum):
    SZFO_1 = "szfo_1"
    SZFO_2 = "szfo_2"
    REGIONAL = "regional"
    MOSCOW = "moscow"
    FOKIN = "fokin"
