"""Persistent /status command for weekly audit cycles."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import CycleStatusView, list_cycle_statuses
from app.bot.keyboards.department import DEPARTMENT_LABELS
from app.domain.models import AuditCycleStatus

# Telegram Bot API hard limit for message text.
TELEGRAM_MESSAGE_LIMIT = 4096


def format_cycle_block(cycle: CycleStatusView) -> str:
    present = ", ".join(
        DEPARTMENT_LABELS[item]
        for item in sorted(cycle.summary.present, key=lambda item: item.value)
    )
    missing = ", ".join(
        DEPARTMENT_LABELS[item]
        for item in sorted(cycle.summary.missing, key=lambda item: item.value)
    )
    if cycle.status == AuditCycleStatus.COLLECTING:
        return "\n".join(
            [
                f"Сбор за {cycle.report_date:%d.%m.%Y}: "
                f"{len(cycle.summary.present)}/5",
                f"Получены: {present or '—'}",
                f"Не хватает: {missing or '—'}",
            ]
        )
    return (
        f"Завершён {cycle.report_date:%d.%m.%Y}: "
        f"5/5, общий долг {cycle.total_debt:,.2f}"
    )


def split_status_messages(
    cycles: list[CycleStatusView],
    *,
    limit: int = TELEGRAM_MESSAGE_LIMIT,
) -> list[str]:
    """Pack cycle blocks into one or more Telegram-safe messages."""
    if not cycles:
        return ["Нет активных аудитов."]

    messages: list[str] = []
    current = ""
    for cycle in cycles:
        block = format_cycle_block(cycle)
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            messages.append(current)
        if len(block) <= limit:
            current = block
            continue
        # Extreme edge: a single cycle block exceeds the limit — hard-split text.
        for start in range(0, len(block), limit):
            messages.append(block[start : start + limit])
        current = ""
    if current:
        messages.append(current)
    return messages


async def handle_status(message: Message, session: AsyncSession) -> None:
    cycles = await list_cycle_statuses(session)
    for text in split_status_messages(cycles):
        await message.answer(text)


def get_status_router() -> Router:
    router = Router(name="status")
    router.message.register(handle_status, Command("status"))
    return router
