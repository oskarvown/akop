"""Stage 1 интеграционный тест: полный `Dispatcher` (allowlist outer middleware +
роутеры) — так же, как собран в `app/main.py`, но с фиктивной HTTP-сессией бота,
без реальных обращений к Telegram API.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatType
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, Message, Update, User

from app.bot.handlers import get_root_router
from app.bot.middlewares.allowlist import AllowlistMiddleware

ALLOWED_USER_ID = 4242
STRANGER_USER_ID = 9999


class FakeSession(BaseSession):
    """Записывает исходящие методы вместо реального похода в Telegram API."""

    def __init__(self) -> None:
        super().__init__()
        self.sent_methods: list[TelegramMethod] = []

    async def close(self) -> None:  # pragma: no cover - нечего закрывать
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: Optional[int] = None,
    ) -> TelegramType:
        self.sent_methods.append(method)
        if isinstance(method, SendMessage):
            return Message(  # type: ignore[return-value]
                message_id=1,
                date=datetime.now(tz=timezone.utc),
                chat=Chat(id=method.chat_id if isinstance(method.chat_id, int) else 0, type="private"),
                text=method.text,
            )
        raise NotImplementedError(f"FakeSession не поддерживает метод {type(method)!r}")

    async def stream_content(  # pragma: no cover - не используется в тесте
        self,
        url: str,
        headers: dict | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ):
        yield b""


def _build_dispatcher() -> Dispatcher:
    """Собирает Dispatcher так же, как `app/main.py`: allowlist — outer middleware
    на `update`, а не на отдельных observer'ах."""
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(
        AllowlistMiddleware(allowed_user_ids={ALLOWED_USER_ID})
    )
    dispatcher.include_router(get_root_router())
    return dispatcher


def _make_start_update(user_id: int, chat_type: str, update_id: int = 1) -> Update:
    chat = Chat(id=1000, type=chat_type)
    user = User(id=user_id, is_bot=False, first_name="Test")
    message = Message(
        message_id=1,
        date=datetime.now(tz=timezone.utc),
        chat=chat,
        from_user=user,
        text="/start",
    )
    return Update(update_id=update_id, message=message)


@pytest.mark.asyncio
async def test_start_available_to_allowed_user_in_private_chat() -> None:
    bot = Bot(token="123456:TEST-TOKEN", session=FakeSession())
    dispatcher = _build_dispatcher()
    update = _make_start_update(ALLOWED_USER_ID, ChatType.PRIVATE)

    await dispatcher.feed_update(bot, update)

    sent = bot.session.sent_methods  # type: ignore[attr-defined]
    assert any(isinstance(method, SendMessage) for method in sent), (
        "Ожидался ответ /start-хендлера разрешённому пользователю в private chat"
    )


@pytest.mark.asyncio
async def test_start_denied_to_allowed_user_in_group() -> None:
    bot = Bot(token="123456:TEST-TOKEN", session=FakeSession())
    dispatcher = _build_dispatcher()
    update = _make_start_update(ALLOWED_USER_ID, ChatType.GROUP)

    await dispatcher.feed_update(bot, update)

    sent = bot.session.sent_methods  # type: ignore[attr-defined]
    assert not sent, "В group-чате /start не должен обрабатываться даже для разрешённого пользователя"


@pytest.mark.asyncio
async def test_start_denied_to_stranger_in_private_chat() -> None:
    bot = Bot(token="123456:TEST-TOKEN", session=FakeSession())
    dispatcher = _build_dispatcher()
    update = _make_start_update(STRANGER_USER_ID, ChatType.PRIVATE)

    await dispatcher.feed_update(bot, update)

    sent = bot.session.sent_methods  # type: ignore[attr-defined]
    assert not sent, "Посторонний пользователь не должен получать ответ /start"
