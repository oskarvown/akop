"""Сохранение результата парсинга в доменные модели (Stage 2).

`ManagerGroup`/`Counterparty` — канонические сущности (get-or-create по
`docs/DATA_CONTRACT.md` §2.3/§7): один и тот же `(department, normalized_name)`
переиспользует существующий `id`, новый не создаётся при повторном появлении
в другом `SourceFile`. `DebtPosition` создаётся заново на каждый файл (это
снимок конкретного отчёта, а не идентичность).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import Department
from app.domain.matching import normalize_name
from app.domain.models import (
    Counterparty,
    DebtPosition,
    ManagerGroup,
    SourceFile,
    SourceFileLifecycle,
    SourceFileStatus,
)
from app.infrastructure.excel.dto import ParsedSourceFile
from app.infrastructure.excel.validator import ValidationResult


async def _get_or_create_manager_group(
    session: AsyncSession, department: Department, raw_name: str
) -> ManagerGroup:
    normalized = normalize_name(raw_name)
    existing = await session.scalar(
        select(ManagerGroup).where(
            ManagerGroup.department == department,
            ManagerGroup.normalized_name == normalized,
        )
    )
    if existing is not None:
        return existing
    group = ManagerGroup(department=department, raw_name=raw_name, normalized_name=normalized)
    session.add(group)
    await session.flush()
    return group


async def _get_or_create_counterparty(
    session: AsyncSession, manager_group_id: int, raw_name: str
) -> Counterparty:
    normalized = normalize_name(raw_name)
    existing = await session.scalar(
        select(Counterparty).where(
            Counterparty.manager_group_id == manager_group_id,
            Counterparty.normalized_name == normalized,
        )
    )
    if existing is not None:
        return existing
    counterparty = Counterparty(
        manager_group_id=manager_group_id, raw_name=raw_name, normalized_name=normalized
    )
    session.add(counterparty)
    await session.flush()
    return counterparty


async def persist_valid_source_file(
    session: AsyncSession,
    *,
    result: ValidationResult,
    department: Department,
    sha256: str,
    original_filename: str | None,
    audit_cycle_id: int | None = None,
    lifecycle_status: SourceFileLifecycle = SourceFileLifecycle.ACTIVE,
) -> SourceFile:
    """Сохраняет успешно провалидированный файл вместе с иерархией строк 1–4.

    `department` — обязательный параметр без значения по умолчанию: эта
    функция никогда не присваивает и не предполагает конкретный отдел
    (в частности, не хардкодит `Department.REGIONAL`) — отдел определяется
    исключительно вызывающей стороной (Telegram-хендлер, знающий, из какого
    диалога/контекста загружен файл). `ManagerGroup`/`Counterparty` создаются
    в границах переданного `department`
    (`uq_manager_group_identity(department, normalized_name)`), поэтому
    одноимённые менеджеры/контрагенты разных отделов не смешиваются.

    Вызывающая сторона обязана убедиться, что `result.is_valid` и `result.parsed`
    заполнены — иначе поднимается `ValueError` (защита от случайного сохранения
    отклонённого файла).

    Семантика `SourceFileStatus.INVALID` в Stage 2: невалидные файлы **вообще не
    сохраняются** в БД (ни `SourceFile`, ни `DebtPosition`) — эта функция явно
    отклоняет попытку. `SourceFileStatus.INVALID` — зарезервированное значение
    схемы для будущего (Stage 3: история/аудит отклонённых загрузок с
    сохранением диагностики без строк), в Stage 2 оно нигде не присваивается.
    """
    if not result.is_valid or result.parsed is None or result.reconciliation is None:
        raise ValueError("persist_valid_source_file вызван для невалидного ValidationResult")

    parsed: ParsedSourceFile = result.parsed

    source_file = SourceFile(
        audit_cycle_id=audit_cycle_id,
        department=department,
        report_date=parsed.report_date,
        sha256=sha256,
        original_filename=original_filename,
        fingerprint_name=result.fingerprint_name,
        status=SourceFileStatus.VALID,
        rejection_reasons=None,
        reported_grand_totals={
            "credit_limit": _decimal_str(parsed.grand_total.credit_limit),
            "document_amount": _decimal_str(parsed.grand_total.document_amount),
            "total_debt": _decimal_str(parsed.grand_total.total_debt),
            "advance": _decimal_str(parsed.grand_total.advance),
            "not_due": _decimal_str(parsed.grand_total.not_due),
            "overdue_1_7": _decimal_str(parsed.grand_total.overdue_1_7),
            "overdue_8_14": _decimal_str(parsed.grand_total.overdue_8_14),
            "overdue_15_21": _decimal_str(parsed.grand_total.overdue_15_21),
            "overdue_22_30": _decimal_str(parsed.grand_total.overdue_22_30),
            "overdue_over_31": _decimal_str(parsed.grand_total.overdue_over_31),
        },
        reconciliation_report=result.reconciliation.as_dict(),
        lifecycle_status=lifecycle_status,
    )
    session.add(source_file)
    await session.flush()

    manager_group_by_row: dict[int, ManagerGroup] = {}
    for mg_row in parsed.manager_groups:
        manager_group_by_row[mg_row.row_index] = await _get_or_create_manager_group(
            session, department, mg_row.raw_label
        )

    counterparty_by_row: dict[int, Counterparty] = {}
    position_by_row: dict[int, DebtPosition] = {}

    for debt_row in parsed.debt_rows:
        manager_group = manager_group_by_row[debt_row.manager_group_row_index]

        if debt_row.outline_level == 1:
            counterparty = await _get_or_create_counterparty(
                session, manager_group.id, debt_row.raw_label
            )
            counterparty_by_row[debt_row.row_index] = counterparty
        else:
            counterparty = counterparty_by_row[debt_row.counterparty_row_index]

        parent_position = (
            position_by_row[debt_row.parent_row_index]
            if debt_row.parent_row_index is not None
            else None
        )

        position = DebtPosition(
            source_file_id=source_file.id,
            manager_group_id=manager_group.id,
            counterparty_id=counterparty.id,
            parent_position_id=parent_position.id if parent_position is not None else None,
            outline_level=debt_row.outline_level,
            row_order=debt_row.row_index,
            raw_label=debt_row.raw_label,
            payment_deferral_days=debt_row.payment_deferral_days,
            payment_deferral_error=debt_row.payment_deferral_error,
            credit_limit=debt_row.credit_limit,
            document_amount=debt_row.document_amount,
            total_debt=debt_row.total_debt,
            advance=debt_row.advance,
            not_due=debt_row.not_due,
            overdue_1_7=debt_row.overdue_1_7,
            overdue_8_14=debt_row.overdue_8_14,
            overdue_15_21=debt_row.overdue_15_21,
            overdue_22_30=debt_row.overdue_22_30,
            overdue_over_31=debt_row.overdue_over_31,
            comment_raw=debt_row.comment_raw,
        )
        session.add(position)
        await session.flush()
        position_by_row[debt_row.row_index] = position

    return source_file


def _decimal_str(value: object) -> str | None:
    return str(value) if value is not None else None
