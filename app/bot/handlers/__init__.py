from aiogram import Router

from app.bot.handlers.report import get_report_router
from app.bot.handlers.start import get_start_router
from app.bot.handlers.status import get_status_router
from app.bot.handlers.upload import get_upload_router


def get_root_router() -> Router:
    """Собирает все хендлеры приложения в один корневой роутер.

    Каждый вызов создаёт новое дерево роутеров (см. `get_start_router`),
    поэтому `get_root_router()` можно безопасно вызывать многократно за
    процесс (актуально для тестов, пересобирающих `Dispatcher`)."""
    root = Router(name="root")
    root.include_router(get_start_router())
    root.include_router(get_status_router())
    root.include_router(get_report_router())
    root.include_router(get_upload_router())
    return root
