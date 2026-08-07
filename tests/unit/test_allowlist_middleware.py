"""Stage 1 тесты `AllowlistMiddleware`.

Мидлварь регистрируется как outer middleware на `dispatcher.update` (см.
`app/main.py` и docstring `AllowlistMiddleware`), поэтому здесь проверяется
именно её решающая логика над `data["event_from_user"]`/`data["event_chat"]` —
теми же полями, которые aiogram заполняет для АБСОЛЮТНО ЛЮБОГО типа события
(message, callback_query и т.д.) до маршрутизации в handler.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Chat, Message, User

from app.bot.middlewares.allowlist import AllowlistMiddleware

ALLOWED_USER_ID = 4242
STRANGER_USER_ID = 9999


def _make_user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def _make_chat(chat_type: str, chat_id: int = 100) -> Chat:
    return Chat(id=chat_id, type=chat_type)


def _make_message(chat: Chat) -> Message:
    return Message(message_id=1, date=datetime.now(tz=timezone.utc), chat=chat)


@pytest.mark.asyncio
async def test_allowed_user_in_private_chat_passes() -> None:
    middleware = AllowlistMiddleware(allowed_user_id=ALLOWED_USER_ID)
    handler = AsyncMock(return_value="handled")
    chat = _make_chat(ChatType.PRIVATE)
    event = _make_message(chat)
    data: dict[str, Any] = {
        "event_from_user": _make_user(ALLOWED_USER_ID),
        "event_chat": chat,
    }

    result = await middleware(handler, event, data)

    assert result == "handled"
    handler.assert_awaited_once_with(event, data)


@pytest.mark.asyncio
async def test_stranger_is_blocked() -> None:
    middleware = AllowlistMiddleware(allowed_user_id=ALLOWED_USER_ID)
    handler = AsyncMock(return_value="handled")
    chat = _make_chat(ChatType.PRIVATE)
    event = _make_message(chat)
    data: dict[str, Any] = {
        "event_from_user": _make_user(STRANGER_USER_ID),
        "event_chat": chat,
    }

    result = await middleware(handler, event, data)

    assert result is None
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", [ChatType.GROUP, ChatType.SUPERGROUP])
async def test_allowed_user_in_group_or_supergroup_is_blocked(chat_type: str) -> None:
    middleware = AllowlistMiddleware(allowed_user_id=ALLOWED_USER_ID)
    handler = AsyncMock(return_value="handled")
    chat = _make_chat(chat_type)
    event = _make_message(chat)
    data: dict[str, Any] = {
        "event_from_user": _make_user(ALLOWED_USER_ID),
        "event_chat": chat,
    }

    result = await middleware(handler, event, data)

    assert result is None
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_unauthorized_callback_query_is_blocked() -> None:
    middleware = AllowlistMiddleware(allowed_user_id=ALLOWED_USER_ID)
    handler = AsyncMock(return_value="handled")
    chat = _make_chat(ChatType.PRIVATE)
    message = _make_message(chat)
    stranger = _make_user(STRANGER_USER_ID)
    event = CallbackQuery(
        id="callback-1",
        from_user=stranger,
        chat_instance="chat-instance-1",
        message=message,
        data="noop",
    )
    data: dict[str, Any] = {
        "event_from_user": stranger,
        "event_chat": chat,
    }

    result = await middleware(handler, event, data)

    assert result is None
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_without_chat_is_not_blocked_by_chat_type() -> None:
    """Апдейты без чата (например, inline_query) не должны отклоняться проверкой
    типа чата — блокируется только по несоответствию пользователя."""
    middleware = AllowlistMiddleware(allowed_user_id=ALLOWED_USER_ID)
    handler = AsyncMock(return_value="handled")
    event = _make_message(_make_chat(ChatType.PRIVATE))
    data: dict[str, Any] = {
        "event_from_user": _make_user(ALLOWED_USER_ID),
        "event_chat": None,
    }

    result = await middleware(handler, event, data)

    assert result == "handled"
    handler.assert_awaited_once()
