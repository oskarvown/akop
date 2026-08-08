"""Background idle-reminder scheduler (PostgreSQL-backed, no Redis)."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.idle_policy import IdleAction
from app.application.idle_reminder_service import (
    claim_reminder,
    claim_still_valid,
    expire_cycle_if_due,
    list_collecting_cycle_ids,
    process_cycle_decision,
    record_successful_reminder,
    release_reminder_claim,
)
from app.bot.keyboards.department import DEPARTMENT_LABELS
from app.bot.messages.status_format import format_reminder_message
from app.config.settings import Settings

logger = logging.getLogger(__name__)

SendMessageFn = Callable[..., Awaitable[object]]


class IdleReminderScheduler:
    """Poll collecting cycles; claim → send → record; per-cycle error isolation."""

    def __init__(
        self,
        *,
        bot: Bot,
        session_maker: async_sessionmaker[AsyncSession],
        settings: Settings,
        send_message: SendMessageFn | None = None,
    ) -> None:
        self._bot = bot
        self._session_maker = session_maker
        self._settings = settings
        self._send_message = send_message or bot.send_message
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        # cycle_id → earliest UTC instant for next claim attempt after errors
        self._backoff_until: dict[int, dt.datetime] = {}

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run_loop(), name="idle-reminder-scheduler")

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
            logger.exception("Idle scheduler stop observed task error")

    async def run_once(self, *, now_utc: dt.datetime | None = None) -> None:
        """Single poll tick (also used by tests)."""
        now = now_utc or dt.datetime.now(dt.timezone.utc)
        async with self._session_maker() as session:
            cycle_ids = await list_collecting_cycle_ids(session)

        for cycle_id in cycle_ids:
            if self._stopped.is_set():
                break
            try:
                await self._process_cycle(cycle_id, now_utc=now)
            except Exception:
                logger.exception(
                    "Idle scheduler failed for cycle_id=%s; continuing", cycle_id
                )

    async def _run_loop(self) -> None:
        poll = self._settings.audit_scheduler_poll_seconds
        try:
            while not self._stopped.is_set():
                try:
                    await self.run_once()
                except Exception:
                    logger.exception("Idle scheduler tick failed")
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=poll)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            return

    def _in_backoff(self, cycle_id: int, now: dt.datetime) -> bool:
        until = self._backoff_until.get(cycle_id)
        return until is not None and now < until

    def _set_backoff(self, cycle_id: int, now: dt.datetime, seconds: float) -> None:
        delay = max(float(seconds), float(self._settings.audit_reminder_error_backoff_seconds))
        self._backoff_until[cycle_id] = now + dt.timedelta(seconds=delay)

    def _clear_backoff(self, cycle_id: int) -> None:
        self._backoff_until.pop(cycle_id, None)

    async def _process_cycle(
        self, cycle_id: int, *, now_utc: dt.datetime
    ) -> None:
        if self._in_backoff(cycle_id, now_utc):
            return

        async with self._session_maker() as session:
            action = await process_cycle_decision(
                session,
                cycle_id=cycle_id,
                settings=self._settings,
                now_utc=now_utc,
            )

        if action == IdleAction.SKIP:
            return
        if action == IdleAction.EXPIRE:
            async with self._session_maker() as session:
                expired = await expire_cycle_if_due(
                    session,
                    cycle_id=cycle_id,
                    settings=self._settings,
                    now_utc=now_utc,
                )
            if expired:
                logger.info("AuditCycle id=%s marked EXPIRED", cycle_id)
            return

        # REMIND
        async with self._session_maker() as session:
            claim = await claim_reminder(
                session,
                cycle_id=cycle_id,
                settings=self._settings,
                now_utc=now_utc,
            )
        if claim is None:
            return

        async with self._session_maker() as session:
            still_ok = await claim_still_valid(
                session, cycle_id=cycle_id, claim_token=claim.claim_token
            )
        if not still_ok:
            return

        text = format_reminder_message(
            report_date=claim.report_date,
            present_count=len(claim.present),
            missing_labels=tuple(
                DEPARTMENT_LABELS[dept]
                for dept in sorted(claim.missing, key=lambda item: item.value)
            ),
        )
        send_timeout = self._settings.audit_reminder_send_timeout_seconds
        try:
            await self._send_with_retry(
                chat_id=claim.notification_chat_id,
                text=text,
                send_timeout=send_timeout,
                claim_ttl=self._settings.audit_reminder_claim_ttl_seconds,
                cycle_id=cycle_id,
                now_utc=now_utc,
            )
        except _PermanentTelegramError as exc:
            logger.warning(
                "Permanent Telegram error for cycle_id=%s: %s", cycle_id, exc
            )
            async with self._session_maker() as session:
                await release_reminder_claim(
                    session, cycle_id=cycle_id, claim_token=claim.claim_token
                )
            self._set_backoff(
                cycle_id,
                now_utc,
                self._settings.audit_reminder_error_backoff_seconds,
            )
            return
        except Exception as exc:  # noqa: BLE001 — classify unknown send failures as temporary
            logger.warning(
                "Temporary Telegram/network error for cycle_id=%s: %s", cycle_id, exc
            )
            async with self._session_maker() as session:
                await release_reminder_claim(
                    session, cycle_id=cycle_id, claim_token=claim.claim_token
                )
            retry_after = getattr(exc, "retry_after", None)
            backoff = self._settings.audit_reminder_error_backoff_seconds
            if isinstance(retry_after, (int, float)):
                backoff = max(backoff, float(retry_after))
            self._set_backoff(cycle_id, now_utc, backoff)
            return

        async with self._session_maker() as session:
            recorded = await record_successful_reminder(
                session,
                cycle_id=cycle_id,
                claim_token=claim.claim_token,
                expected_reminder_count=claim.reminder_count,
            )
        if recorded:
            self._clear_backoff(cycle_id)
            logger.info(
                "Recorded reminder for cycle_id=%s count=%s",
                cycle_id,
                claim.reminder_count + 1,
            )
        else:
            logger.info(
                "Reminder record no-op for cycle_id=%s (stale claim/activity)",
                cycle_id,
            )

    async def _send_with_retry(
        self,
        *,
        chat_id: int,
        text: str,
        send_timeout: int,
        claim_ttl: int,
        cycle_id: int,
        now_utc: dt.datetime,
    ) -> None:
        """Send with timeout < claim TTL; honour RetryAfter within remaining lease."""
        deadline = now_utc + dt.timedelta(seconds=min(send_timeout, claim_ttl - 1))

        async def _once() -> None:
            remaining = (deadline - dt.datetime.now(dt.timezone.utc)).total_seconds()
            if remaining <= 0:
                raise TimeoutError("reminder send deadline exceeded")
            await asyncio.wait_for(
                self._send_message(chat_id=chat_id, text=text),
                timeout=remaining,
            )

        try:
            await _once()
            return
        except TelegramRetryAfter as exc:
            wait_for = float(exc.retry_after)
            remaining = (deadline - dt.datetime.now(dt.timezone.utc)).total_seconds()
            if wait_for >= remaining:
                # Cannot honour RetryAfter inside lease — surface as temporary.
                raise
            await asyncio.sleep(wait_for)
            await _once()
            return
        except TelegramNetworkError:
            raise
        except TelegramAPIError as exc:
            # Treat most API errors as permanent for backoff purposes
            # (blocked bot, chat not found, etc.). RetryAfter already handled.
            raise _PermanentTelegramError(str(exc)) from exc


class _PermanentTelegramError(Exception):
    pass
