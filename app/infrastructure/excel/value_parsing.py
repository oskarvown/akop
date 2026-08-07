"""Приведение значений ячеек к типам контракта (`docs/DATA_CONTRACT.md` §5, §6.1).

Пустая ячейка ≠ 0 (`None`, а не `Decimal("0")`) — различие между «нет данных»
и «ноль» сохраняется до уровня reconciliation.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class CellTypeError(ValueError):
    """Значение ячейки не приводится к ожидаемому типу."""


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def parse_decimal(value: object) -> Decimal | None:
    """Денежная колонка: `Decimal | None`. Нечисловое значение — `CellTypeError`."""
    if _is_blank(value):
        return None
    if isinstance(value, bool):
        raise CellTypeError(f"Ожидалось число, получено bool: {value!r}")
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value)).quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise CellTypeError(f"Не удалось привести {value!r} к Decimal") from exc
    if isinstance(value, str):
        try:
            return Decimal(value.strip().replace(",", ".")).quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise CellTypeError(f"Не удалось привести строку {value!r} к Decimal") from exc
    raise CellTypeError(f"Неподдерживаемый тип значения денежной колонки: {type(value)!r}")


@dataclass(frozen=True)
class PaymentDeferralResult:
    """Результат разбора «Отсрочка платежа» — ошибка не блокирует файл (§6.1)."""

    days: int | None
    error: str | None = None


def parse_payment_deferral_days(value: object) -> PaymentDeferralResult:
    """`payment_deferral_days: int | null` — ошибка уровня записи, не файла (§6.1)."""
    if _is_blank(value):
        return PaymentDeferralResult(days=None)
    if isinstance(value, bool):
        return PaymentDeferralResult(days=None, error=f"Недопустимое значение отсрочки: {value!r}")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            return PaymentDeferralResult(
                days=None, error=f"Дробное значение отсрочки платежа: {value!r}"
            )
        days = int(value)
        if days < 0:
            return PaymentDeferralResult(
                days=None, error=f"Отрицательное значение отсрочки платежа: {days!r}"
            )
        return PaymentDeferralResult(days=days)
    if isinstance(value, str):
        text = value.strip()
        try:
            as_float = float(text.replace(",", "."))
        except ValueError:
            return PaymentDeferralResult(
                days=None, error=f"Нечисловое значение отсрочки платежа: {value!r}"
            )
        return parse_payment_deferral_days(as_float)
    return PaymentDeferralResult(days=None, error=f"Неподдерживаемый тип отсрочки платежа: {type(value)!r}")


def parse_comment(value: object) -> str | None:
    if _is_blank(value):
        return None
    return str(value).strip()


def parse_label(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()
