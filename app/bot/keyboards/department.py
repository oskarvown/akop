"""Department selection keyboard."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.domain.enums import Department


DEPARTMENT_LABELS: dict[Department, str] = {
    Department.SZFO_1: "СЗФО-1",
    Department.SZFO_2: "СЗФО-2",
    Department.REGIONAL: "Региональный",
    Department.MOSCOW: "Москва",
    Department.FOKIN: "Фокин",
}


def department_keyboard(upload_token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=DEPARTMENT_LABELS[department],
                    callback_data=f"dept:{upload_token}:{department.value}",
                )
            ]
            for department in Department
        ]
    )
