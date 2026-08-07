"""Parallel Telegram document updates through a real Dispatcher."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatType
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, Document, Message, Update, User
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.main import create_dispatcher
from tests.fixtures.generate_regional_fixtures import _basic_spec
from tests.fixtures.regional_builder import build_regional_xls

ALLOWED_USER_ID = 4242
CHAT_ID = 1000
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "regional"
VALID_FILE = FIXTURES / "regional_valid_basic.xls"


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.sent_methods: list[TelegramMethod[Any]] = []

    async def close(self) -> None:  # pragma: no cover
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: Optional[int] = None,
    ) -> TelegramType:
        self.sent_methods.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.sent_methods),
                date=datetime.now(tz=timezone.utc),
                chat=Chat(
                    id=method.chat_id if isinstance(method.chat_id, int) else 0,
                    type=ChatType.PRIVATE,
                ),
                text=method.text,
            )
        raise NotImplementedError(f"Unsupported method {type(method)!r}")

    async def stream_content(  # pragma: no cover
        self,
        url: str,
        headers: dict | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ):
        yield b""


def _document_update(
    *,
    update_id: int,
    message_id: int,
    file_id: str,
    file_name: str,
    file_size: int,
) -> Update:
    user = User(id=ALLOWED_USER_ID, is_bot=False, first_name="Askar")
    chat = Chat(id=CHAT_ID, type=ChatType.PRIVATE)
    document = Document(
        file_id=file_id,
        file_unique_id=f"uniq-{file_id}",
        file_name=file_name,
        file_size=file_size,
    )
    message = Message(
        message_id=message_id,
        date=datetime.now(tz=timezone.utc),
        chat=chat,
        from_user=user,
        document=document,
    )
    return Update(update_id=update_id, message=message)


@pytest.mark.asyncio
async def test_parallel_document_updates_reject_second_while_first_open(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Two concurrent document Updates for the same chat must not both open FSM.

    With SimpleEventIsolation the second update waits for the first, then sees
    non-empty FSM and answers with the «ещё не завершена» warning.
    """
    other_path = tmp_path / "other_date.xls"
    build_regional_xls(_basic_spec("15.07.2026"), other_path)

    payloads = {
        "file-a": VALID_FILE.read_bytes(),
        "file-b": other_path.read_bytes(),
    }

    session = RecordingSession()
    bot = Bot(token="123456:TEST-TOKEN", session=session)

    async def _download(file: Document, destination: Path, **_: Any) -> Path:
        destination = Path(destination)
        # Yield so both feed_update tasks are running before download finishes.
        await asyncio.sleep(0)
        destination.write_bytes(payloads[file.file_id])
        return destination

    bot.download = _download  # type: ignore[method-assign]

    dispatcher = create_dispatcher(
        allowed_user_id=ALLOWED_USER_ID,
        session_maker=stage3_session_maker,
    )

    update_a = _document_update(
        update_id=1,
        message_id=1,
        file_id="file-a",
        file_name=VALID_FILE.name,
        file_size=len(payloads["file-a"]),
    )
    update_b = _document_update(
        update_id=2,
        message_id=2,
        file_id="file-b",
        file_name=other_path.name,
        file_size=len(payloads["file-b"]),
    )

    await asyncio.gather(
        dispatcher.feed_update(bot, update_a),
        dispatcher.feed_update(bot, update_b),
    )

    texts = [
        method.text or ""
        for method in session.sent_methods
        if isinstance(method, SendMessage)
    ]
    choose_dept = [text for text in texts if "Выберите отдел" in text]
    busy = [text for text in texts if "ещё не завершена" in text]

    assert len(choose_dept) == 1, texts
    assert len(busy) == 1, texts
    assert any(VALID_FILE.name in text or other_path.name in text for text in busy)
