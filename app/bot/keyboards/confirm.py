"""Confirmation keyboards carrying an upload token."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def replacement_keyboard(upload_token: str) -> InlineKeyboardMarkup:
    return _confirmation_keyboard(
        confirm_text="Заменить",
        cancel_text="Оставить старый",
        prefix="replace",
        upload_token=upload_token,
    )


def new_cycle_keyboard(upload_token: str) -> InlineKeyboardMarkup:
    return _confirmation_keyboard(
        confirm_text="Создать отдельный цикл",
        cancel_text="Отмена",
        prefix="newcycle",
        upload_token=upload_token,
    )


def _confirmation_keyboard(
    *,
    confirm_text: str,
    cancel_text: str,
    prefix: str,
    upload_token: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=confirm_text,
                    callback_data=f"{prefix}:{upload_token}:confirm",
                ),
                InlineKeyboardButton(
                    text=cancel_text,
                    callback_data=f"{prefix}:{upload_token}:cancel",
                ),
            ]
        ]
    )
