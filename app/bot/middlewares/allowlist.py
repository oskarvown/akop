"""Allowlist-мидлварь: бот закрытый, отвечает только разрешённым user_id в private chat.

Регистрируется как **outer middleware на observer `update`**
(`dispatcher.update.outer_middleware(...)`), а не на отдельных observer'ах
(`message`, `callback_query` и т.д.). Это принципиально: `update` — общая точка
входа для АБСОЛЮТНО ЛЮБОГО типа Telegram-события (message, callback_query,
inline_query, chat_member, my_chat_member и т.д.), и авторизация выполняется
до того, как апдейт будет передан в какой-либо router/handler. Ни один
будущий handler не может обойти проверку, зарегистрировавшись на другом
наблюдателе диспетчера — see `app/main.py`.

Проверка опирается на `data["event_from_user"]` / `data["event_chat"]`,
которые аiogram заполняет своей встроенной `UserContextMiddleware`
(тоже outer middleware на `update`, регистрируется первой в `Dispatcher.__init__`
до пользовательского кода) — поэтому эти поля уже доступны на момент вызова
нашей мидлвари.

Правила (Stage 1 clarification: бот работает только в private chat; allowlist
может содержать несколько Telegram user_id):

1. Апдейты без определённого пользователя или от чужого `user_id` — отклоняются.
2. Апдейты с определённым чатом, чей `type != private` (group/supergroup/channel),
   отклоняются даже для разрешённого пользователя.
3. Апдейты без привязанного чата (например, `inline_query`) чат не проверяют
   (проверка групп/супергрупп для них неприменима).
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Chat, TelegramObject, User

logger = logging.getLogger(__name__)


class AllowlistMiddleware(BaseMiddleware):
    """Пропускает только апдейты разрешённых пользователей в private chat."""

    def __init__(self, allowed_user_ids: Iterable[int]) -> None:
        self._allowed_user_ids = frozenset(allowed_user_ids)
        if not self._allowed_user_ids:
            raise ValueError("allowed_user_ids must not be empty")

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        chat: Chat | None = data.get("event_chat")

        if user is None or user.id not in self._allowed_user_ids:
            logger.warning(
                "Отклонён апдейт от неразрешённого пользователя: user_id=%s",
                user.id if user else None,
            )
            return None

        if chat is not None and chat.type != ChatType.PRIVATE:
            logger.warning(
                "Отклонён апдейт от разрешённого пользователя (user_id=%s) вне "
                "private chat: chat_id=%s, chat_type=%s",
                user.id,
                chat.id,
                chat.type,
            )
            return None

        return await handler(event, data)
