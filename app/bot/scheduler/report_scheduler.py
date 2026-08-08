"""Background report build + Telegram delivery scheduler (Stage 4.3).

Telegram I/O runs only after claim transactions commit. Delivery is at-least-once:
if document_message_id is already recorded, send_document is skipped and the
lifecycle completes as DELIVERED.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import BufferedInputFile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.report_delivery_service import (
    claim_delivery,
    complete_delivery,
    fail_delivery,
    list_due_delivery_ids,
    load_delivery_send_context,
    record_document_sent,
    record_summary_message_sent,
    recover_missing_automatic_deliveries,
    recover_stale_claimed_deliveries,
)
from app.application.report_service import (
    claim_report_build,
    complete_report_build,
    fail_report_build,
    prepare_buildable_report_ids,
    recover_missing_reports,
    recover_stale_building_reports,
    run_claimed_build,
)
from app.config.settings import Settings

logger = logging.getLogger(__name__)

SendMessageFn = Callable[..., Awaitable[object]]
SendDocumentFn = Callable[..., Awaitable[object]]


class ReportScheduler:
    """Poll: recover → build reports → deliver Telegram CORE/manual outbox."""

    def __init__(
        self,
        *,
        bot: Bot,
        session_maker: async_sessionmaker[AsyncSession],
        settings: Settings,
        send_message: SendMessageFn | None = None,
        send_document: SendDocumentFn | None = None,
    ) -> None:
        self._bot = bot
        self._session_maker = session_maker
        self._settings = settings
        self._send_message = send_message or bot.send_message
        self._send_document = send_document or bot.send_document
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run_loop(), name="report-scheduler")

    async def stop(self, *, timeout: float = 5.0) -> None:
        self._stopped.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            logger.exception("Report scheduler stop observed task error")

    async def run_once(self, *, now_utc: dt.datetime | None = None) -> None:
        now = now_utc or dt.datetime.now(dt.timezone.utc)
        async with self._session_maker() as session:
            await recover_missing_reports(session)
        async with self._session_maker() as session:
            await recover_stale_building_reports(
                session, settings=self._settings, now_utc=now
            )

        buildable = await prepare_buildable_report_ids(
            self._session_maker, settings=self._settings, now_utc=now
        )
        for report_id in buildable:
            if self._stopped.is_set():
                break
            try:
                await self._build_one(report_id, now_utc=now)
            except Exception:
                logger.exception(
                    "Report build failed for report_id=%s; continuing", report_id
                )

        async with self._session_maker() as session:
            await recover_missing_automatic_deliveries(session)
        async with self._session_maker() as session:
            await recover_stale_claimed_deliveries(
                session, settings=self._settings, now_utc=now
            )

        async with self._session_maker() as session:
            due = await list_due_delivery_ids(
                session, settings=self._settings, now_utc=now
            )
        for delivery_id in due:
            if self._stopped.is_set():
                break
            try:
                await self._deliver_one(delivery_id, now_utc=now)
            except Exception:
                logger.exception(
                    "Report delivery failed for delivery_id=%s; continuing",
                    delivery_id,
                )

    async def _run_loop(self) -> None:
        poll = self._settings.report_scheduler_poll_seconds
        try:
            while not self._stopped.is_set():
                try:
                    await self.run_once()
                except Exception:
                    logger.exception("Report scheduler tick failed")
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=poll)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            return

    async def _build_one(self, report_id: int, *, now_utc: dt.datetime) -> None:
        async with self._session_maker() as session:
            claim = await claim_report_build(
                session,
                report_id=report_id,
                settings=self._settings,
                now_utc=now_utc,
            )
        if claim is None:
            return
        try:
            async with self._session_maker() as session:
                result = await run_claimed_build(session, claim=claim)
            async with self._session_maker() as session:
                await complete_report_build(
                    session,
                    report_id=claim.report_id,
                    claim_token=claim.claim_token,
                    summary_json=result.summary_json,
                    excel_bytes=result.excel_bytes,
                    excel_sha256=result.excel_sha256,
                )
        except Exception as exc:
            async with self._session_maker() as session:
                await fail_report_build(
                    session,
                    report_id=claim.report_id,
                    claim_token=claim.claim_token,
                    error=str(exc),
                    settings=self._settings,
                    now_utc=now_utc,
                )
            raise

    async def _deliver_one(self, delivery_id: int, *, now_utc: dt.datetime) -> None:
        async with self._session_maker() as session:
            claim = await claim_delivery(
                session,
                delivery_id=delivery_id,
                settings=self._settings,
                now_utc=now_utc,
            )
        if claim is None:
            return

        async with self._session_maker() as session:
            ctx = await load_delivery_send_context(session, delivery_id=delivery_id)
        if ctx is None:
            async with self._session_maker() as session:
                await fail_delivery(
                    session,
                    delivery_id=delivery_id,
                    claim_token=claim.claim_token,
                    error="delivery_context_missing",
                    settings=self._settings,
                    now_utc=now_utc,
                )
            return

        # Already recorded document → finish without re-send.
        if ctx.document_already_sent:
            async with self._session_maker() as session:
                await complete_delivery(
                    session,
                    delivery_id=delivery_id,
                    claim_token=claim.claim_token,
                )
            return

        excel = ctx.excel_bytes
        digest = hashlib.sha256(excel).hexdigest()
        max_bytes = int(self._settings.report_delivery_max_file_bytes)
        if (
            not excel
            or digest != ctx.excel_sha256
            or len(excel) > max_bytes
        ):
            async with self._session_maker() as session:
                await fail_delivery(
                    session,
                    delivery_id=delivery_id,
                    claim_token=claim.claim_token,
                    error="artifact_bytes_invalid_or_too_large",
                    settings=self._settings,
                    now_utc=now_utc,
                )
            return

        send_timeout = float(self._settings.report_delivery_send_timeout_seconds)
        try:
            async with asyncio.timeout(send_timeout):
                for text in ctx.summary_messages:
                    result = await self._send_message(
                        chat_id=claim.destination_chat_id,
                        text=text,
                        parse_mode=None,
                    )
                    message_id = getattr(result, "message_id", None)
                    if message_id is None:
                        raise RuntimeError("summary send missing message_id")
                    async with self._session_maker() as session:
                        ok = await record_summary_message_sent(
                            session,
                            delivery_id=delivery_id,
                            claim_token=claim.claim_token,
                            message_id=int(message_id),
                        )
                    if not ok:
                        return

                document = BufferedInputFile(excel, filename=ctx.filename)
                doc_result = await self._send_document(
                    chat_id=claim.destination_chat_id,
                    document=document,
                    caption=ctx.caption,
                    parse_mode=None,
                )
                doc_message_id = getattr(doc_result, "message_id", None)
                if doc_message_id is None:
                    raise RuntimeError("document send missing message_id")
                async with self._session_maker() as session:
                    ok = await record_document_sent(
                        session,
                        delivery_id=delivery_id,
                        claim_token=claim.claim_token,
                        message_id=int(doc_message_id),
                    )
                if not ok:
                    return
                async with self._session_maker() as session:
                    await complete_delivery(
                        session,
                        delivery_id=delivery_id,
                        claim_token=claim.claim_token,
                    )
        except (TelegramRetryAfter, TelegramNetworkError, TelegramAPIError, TimeoutError, Exception) as exc:
            logger.warning(
                "Telegram delivery error delivery_id=%s: %s", delivery_id, exc
            )
            async with self._session_maker() as session:
                await fail_delivery(
                    session,
                    delivery_id=delivery_id,
                    claim_token=claim.claim_token,
                    error=str(exc),
                    settings=self._settings,
                    now_utc=now_utc,
                )
