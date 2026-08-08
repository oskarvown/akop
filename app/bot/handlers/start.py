"""Health-check хендлер /start (Stage 1: подтверждает, что каркас запущен)."""
from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message


async def handle_start(message: Message) -> None:
    await message.answer(
        "Дебиторка-бот запущен.\n"
        "Отправьте Excel-файл для недельного аудита или используйте /status.\n"
        "Готовый отчёт: /report (или /report core, /report YYYY-MM-DD).\n"
        "После сохранения в открытый сбор можно отменить последнюю загрузку "
        "кнопкой или /undo (с подтверждением)."
    )


def get_start_router() -> Router:
    """Фабрика роутера (не module-level singleton): `Router` в aiogram можно
    прикрепить только к одному родителю за раз, а фабрика позволяет безопасно
    пересобирать `Dispatcher` несколько раз за процесс (актуально для тестов)."""
    router = Router(name="start")
    router.message.register(handle_start, CommandStart())
    return router
