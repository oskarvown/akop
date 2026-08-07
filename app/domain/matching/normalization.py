"""Нормализация имён `ManagerGroup`/`Counterparty` — см. `docs/DATA_CONTRACT.md` §2.3, §7.

Полная нормализация (кавычки, ОПФ и т.п. — Roadmap §4.4) относится к Stage 3+
(matching между неделями). Stage 2 использует только базовый, детерминированный
уровень нормализации, необходимый для устойчивого канонического ключа
`department_id + normalized_name`: NFKC, обрезка пробелов по краям, схлопывание
внутренних пробелов, приведение регистра.
"""
from __future__ import annotations

import re
import unicodedata

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_name(raw_name: str) -> str:
    """Базовая нормализация строкового имени для канонического ключа идентичности."""
    text = unicodedata.normalize("NFKC", raw_name)
    text = text.strip()
    text = _WHITESPACE_RE.sub(" ", text)
    return text.casefold()
