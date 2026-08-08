"""Manual /report command — Telegram delivery of CORE (or latest ENRICHED)."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import re
from dataclasses import dataclass

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.report_delivery_service import (
    ResolveStatus,
    claim_delivery,
    complete_delivery,
    create_manual_delivery,
    fail_delivery,
    load_delivery_send_context,
    record_document_sent,
    record_summary_message_sent,
    resolve_report_artifact,
)
from app.config import get_settings

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class ReportCommandArgs:
    force_core: bool
    report_date: dt.date | None


class ReportArgsError(ValueError):
    pass


def _parse_report_date(token: str) -> dt.date:
    if not _DATE_RE.match(token):
        raise ReportArgsError("invalid")
    try:
        return dt.date.fromisoformat(token)
    except ValueError:
        raise ReportArgsError("invalid") from None


def parse_report_args(args: str | None) -> ReportCommandArgs:
    """Strict parser: empty | YYYY-MM-DD | core | core YYYY-MM-DD."""
    tokens = (args or "").split()
    if not tokens:
        return ReportCommandArgs(force_core=False, report_date=None)
    if tokens[0].lower() == "core":
        if len(tokens) == 1:
            return ReportCommandArgs(force_core=True, report_date=None)
        if len(tokens) == 2:
            return ReportCommandArgs(
                force_core=True, report_date=_parse_report_date(tokens[1])
            )
        raise ReportArgsError("invalid")
    if len(tokens) == 1:
        return ReportCommandArgs(
            force_core=False, report_date=_parse_report_date(tokens[0])
        )
    raise ReportArgsError("invalid")


async def handle_report(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    bot: Bot,
) -> None:
    try:
        parsed = parse_report_args(command.args)
    except ReportArgsError:
        await message.answer(
            "Неверный формат. Используйте:\n"
            "/report\n"
            "/report YYYY-MM-DD\n"
            "/report core\n"
            "/report core YYYY-MM-DD",
            parse_mode=None,
        )
        return

    resolved = await resolve_report_artifact(
        session,
        report_date=parsed.report_date,
        force_core=parsed.force_core,
    )
    if not resolved.ready or resolved.artifact_id is None:
        await message.answer(resolved.user_message, parse_mode=None)
        return

    if message.chat is None or message.from_user is None:
        await message.answer("Не удалось определить чат отправителя.", parse_mode=None)
        return

    settings = get_settings()
    delivery_id = await create_manual_delivery(
        session,
        artifact_id=resolved.artifact_id,
        destination_chat_id=message.chat.id,
        requested_by_user_id=message.from_user.id,
    )
    claim = await claim_delivery(
        session, delivery_id=delivery_id, settings=settings
    )
    if claim is None:
        await message.answer(
            "Не удалось начать отправку отчёта. Попробуйте позже.",
            parse_mode=None,
        )
        return

    ctx = await load_delivery_send_context(session, delivery_id=delivery_id)
    if ctx is None:
        await fail_delivery(
            session,
            delivery_id=delivery_id,
            claim_token=claim.claim_token,
            error="delivery_context_missing",
            settings=settings,
        )
        await message.answer("Ошибка подготовки отчёта.", parse_mode=None)
        return

    excel = ctx.excel_bytes
    digest = hashlib.sha256(excel).hexdigest()
    if (
        not excel
        or digest != ctx.excel_sha256
        or len(excel) > settings.report_delivery_max_file_bytes
    ):
        await fail_delivery(
            session,
            delivery_id=delivery_id,
            claim_token=claim.claim_token,
            error="artifact_bytes_invalid_or_too_large",
            settings=settings,
        )
        await message.answer(
            "Файл отчёта повреждён или слишком большой для отправки.",
            parse_mode=None,
        )
        return

    send_timeout = float(settings.report_delivery_send_timeout_seconds)
    try:
        async with asyncio.timeout(send_timeout):
            for text in ctx.summary_messages:
                sent = await bot.send_message(
                    chat_id=claim.destination_chat_id,
                    text=text,
                    parse_mode=None,
                )
                ok = await record_summary_message_sent(
                    session,
                    delivery_id=delivery_id,
                    claim_token=claim.claim_token,
                    message_id=sent.message_id,
                )
                if not ok:
                    await message.answer(
                        "Отправка прервана (конфликт доставки). Повторите /report.",
                        parse_mode=None,
                    )
                    return

            doc = await bot.send_document(
                chat_id=claim.destination_chat_id,
                document=BufferedInputFile(excel, filename=ctx.filename),
                caption=ctx.caption,
                parse_mode=None,
            )
            ok = await record_document_sent(
                session,
                delivery_id=delivery_id,
                claim_token=claim.claim_token,
                message_id=doc.message_id,
            )
            if not ok:
                await message.answer(
                    "Документ отправлен, но фиксация доставки не удалась. "
                    "При повторе документ может не отправиться повторно.",
                    parse_mode=None,
                )
                return
            await complete_delivery(
                session, delivery_id=delivery_id, claim_token=claim.claim_token
            )
    except TimeoutError as exc:
        logger.warning("Manual /report delivery timed out after %ss", send_timeout)
        await fail_delivery(
            session,
            delivery_id=delivery_id,
            claim_token=claim.claim_token,
            error=str(exc) or "delivery_send_timeout",
            settings=settings,
        )
        max_attempts = settings.report_delivery_max_attempts
        if claim.attempt_count >= max_attempts:
            await message.answer(
                "Не удалось отправить отчёт. Повторите /report позже.",
                parse_mode=None,
            )
        else:
            await message.answer(
                "Отправка не удалась; повтор будет выполнен автоматически.",
                parse_mode=None,
            )
        return
    except Exception as exc:
        logger.exception("Manual /report delivery failed")
        await fail_delivery(
            session,
            delivery_id=delivery_id,
            claim_token=claim.claim_token,
            error=str(exc),
            settings=settings,
        )
        max_attempts = settings.report_delivery_max_attempts
        # attempt_count already incremented by claim
        if claim.attempt_count >= max_attempts:
            await message.answer(
                "Не удалось отправить отчёт. Повторите /report позже.",
                parse_mode=None,
            )
        else:
            await message.answer(
                "Отправка не удалась; повтор будет выполнен автоматически.",
                parse_mode=None,
            )


def get_report_router() -> Router:
    router = Router(name="report")
    router.message.register(handle_report, Command("report"))
    return router
