"""Deterministic manager-comment parser (Stage 4.4). No I/O, no Stage 6 statuses."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum


class CommentParseOutcome(str, Enum):
    EMPTY = "empty"
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class CommentParseResult:
    outcome: CommentParseOutcome
    mentioned_date: dt.date | None = None
    mentioned_amount: Decimal | None = None
    action: str | None = None
    reason: str | None = None
    responsible_person: str | None = None
    summary: str | None = None
    confidence: str = "none"
    parse_notes: str | None = None


_DATE_DMY = re.compile(
    r"(?P<d>\d{1,2})[./](?P<m>\d{1,2})(?:[./](?P<y>\d{2,4}))?"
)
_AMOUNT = re.compile(
    r"(?<!\d)(\d{1,3}(?:[ \u00a0]\d{3})+)(?:[,.](\d{1,2}))?\s*(?:₽|руб\.?|RUB)?"
    r"|(?<!\d)(\d+)(?:[,.](\d{1,2}))?\s*(?:₽|руб\.?|RUB)",
    re.IGNORECASE,
)
_PERSON = re.compile(
    r"(?:менеджер|отв\.?|ответственный)\s*[:\-]?\s*"
    r"([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-\s]{1,60})",
    re.IGNORECASE,
)
_ACTION_HINTS = (
    ("оплат", "оплата"),
    ("перенос", "перенос"),
    ("ждем", "ожидание"),
    ("ждём", "ожидание"),
    ("обеща", "обещание_даты"),
    ("счет", "счёт"),
    ("счёт", "счёт"),
)
_REASON_HINTS = (
    ("задерж", "задержка"),
    ("касс", "касса"),
    ("банк", "банк"),
    ("отгруз", "отгрузка"),
)


def apply_report_year_rule(
    day: int, month: int, year: int | None, *, report_date: dt.date
) -> dt.date | None:
    """DATA_CONTRACT: missing year → report year; if already passed → next year."""
    try:
        if year is None:
            candidate = dt.date(report_date.year, month, day)
            if candidate < report_date:
                candidate = dt.date(report_date.year + 1, month, day)
            return candidate
        if year < 100:
            year += 2000
        return dt.date(year, month, day)
    except ValueError:
        return None


def parse_comment(comment_raw: str | None, *, report_date: dt.date) -> CommentParseResult:
    text = (comment_raw or "").strip()
    if not text:
        return CommentParseResult(outcome=CommentParseOutcome.EMPTY)

    mentioned_date: dt.date | None = None
    date_match = _DATE_DMY.search(text)
    if date_match:
        day = int(date_match.group("d"))
        month = int(date_match.group("m"))
        y_raw = date_match.group("y")
        year = int(y_raw) if y_raw else None
        mentioned_date = apply_report_year_rule(
            day, month, year, report_date=report_date
        )

    mentioned_amount: Decimal | None = None
    # Prefer amounts with thousand separators or explicit currency to avoid date false-positives.
    amount_match = _AMOUNT.search(text)
    if amount_match:
        whole = amount_match.group(1) or amount_match.group(3)
        frac = amount_match.group(2) or amount_match.group(4) or "0"
        assert whole is not None
        whole = whole.replace(" ", "").replace("\u00a0", "")
        try:
            mentioned_amount = Decimal(f"{whole}.{frac}")
        except InvalidOperation:
            mentioned_amount = None

    action = None
    lower = text.lower()
    for needle, label in _ACTION_HINTS:
        if needle in lower:
            action = label
            break

    reason = None
    for needle, label in _REASON_HINTS:
        if needle in lower:
            reason = label
            break

    responsible_person = None
    person_match = _PERSON.search(text)
    if person_match:
        responsible_person = person_match.group(1).strip()

    extracted = any(
        v is not None
        for v in (mentioned_date, mentioned_amount, action, reason, responsible_person)
    )
    if not extracted:
        return CommentParseResult(
            outcome=CommentParseOutcome.AMBIGUOUS,
            summary=text[:200],
            confidence="none",
            parse_notes="no_deterministic_facts",
        )

    summary_parts: list[str] = []
    if action:
        summary_parts.append(action)
    if mentioned_date:
        summary_parts.append(mentioned_date.isoformat())
    if mentioned_amount is not None:
        summary_parts.append(str(mentioned_amount))
    summary = ", ".join(summary_parts) if summary_parts else text[:200]

    confidence = "high" if mentioned_date or mentioned_amount is not None else "medium"
    return CommentParseResult(
        outcome=CommentParseOutcome.RESOLVED,
        mentioned_date=mentioned_date,
        mentioned_amount=mentioned_amount,
        action=action,
        reason=reason,
        responsible_person=responsible_person,
        summary=summary,
        confidence=confidence,
    )
