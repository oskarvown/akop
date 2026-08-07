"""Persistent /status command for weekly audit cycles."""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import list_cycle_statuses
from app.bot.keyboards.department import DEPARTMENT_LABELS
from app.domain.models import AuditCycleStatus


async def handle_status(message: Message, session: AsyncSession) -> None:
    cycles = await list_cycle_statuses(session)
    if not cycles:
        await message.answer("Нет активных аудитов.")
        return

    lines: list[str] = []
    for cycle in cycles:
        present = ", ".join(
            DEPARTMENT_LABELS[item]
            for item in sorted(cycle.summary.present, key=lambda item: item.value)
        )
        missing = ", ".join(
            DEPARTMENT_LABELS[item]
            for item in sorted(cycle.summary.missing, key=lambda item: item.value)
        )
        if cycle.status == AuditCycleStatus.COLLECTING:
            lines.extend(
                [
                    f"Сбор за {cycle.report_date:%d.%m.%Y}: "
                    f"{len(cycle.summary.present)}/5",
                    f"Получены: {present or '—'}",
                    f"Не хватает: {missing or '—'}",
                ]
            )
        else:
            lines.append(
                f"Завершён {cycle.report_date:%d.%m.%Y}: "
                f"5/5, общий долг {cycle.total_debt:,.2f}"
            )
        lines.append("")

    await message.answer("\n".join(lines).rstrip())


def get_status_router() -> Router:
    router = Router(name="status")
    router.message.register(handle_status, Command("status"))
    return router
