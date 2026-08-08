"""Stage 3.2 idle reminders / EXPIRED — PostgreSQL integration tests."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramNetworkError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.audit_service import (
    CycleImmutableError,
    add_source_file_atomic,
    list_cycle_statuses,
)
from app.application.idle_reminder_service import (
    claim_reminder,
    expire_cycle_if_due,
    record_successful_reminder,
    release_reminder_claim,
)
from app.bot.handlers.status import format_cycle_block
from app.bot.keyboards.department import DEPARTMENT_LABELS
from app.bot.messages.status_format import format_reminder_message
from app.bot.scheduler.idle_scheduler import IdleReminderScheduler
from app.domain.enums import Department
from app.domain.models import AuditCycle, AuditCycleStatus, SourceFileLifecycle
from app.infrastructure.excel.validator import (
    ValidationResult,
    validate_confirmed_template_file,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "regional"
    / "regional_valid_basic.xls"
)
REPORT_DATE = dt.date(2026, 8, 1)
NOTIFY_CHAT = 743971617
OTHER_USER = 6840100810
UTC = dt.timezone.utc


def _settings(**overrides: object) -> SimpleNamespace:
    base = {
        "audit_idle_timeout_seconds": 3600,
        "audit_reminder_interval_seconds": 3600,
        "audit_max_reminders": 2,
        "audit_expire_grace_seconds": 86400,
        "audit_notification_chat_id": NOTIFY_CHAT,
        "audit_scheduler_poll_seconds": 60,
        "audit_reminder_claim_ttl_seconds": 300,
        "audit_reminder_send_timeout_seconds": 30,
        "audit_reminder_error_backoff_seconds": 900,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def valid_result() -> ValidationResult:
    result = validate_confirmed_template_file(FIXTURE)
    assert result.is_valid and result.parsed is not None
    return result


def result_for_date(result: ValidationResult, report_date: dt.date) -> ValidationResult:
    assert result.parsed is not None
    return replace(result, parsed=replace(result.parsed, report_date=report_date))


async def seed_collecting(
    session: AsyncSession,
    result: ValidationResult,
    *,
    sha: str = "idle-seed",
    report_date: dt.date = REPORT_DATE,
    department: Department = Department.REGIONAL,
):
    dated = result_for_date(result, report_date)
    return await add_source_file_atomic(
        session,
        result=dated,
        department=department,
        sha256=sha,
        original_filename=f"{sha}.xls",
        report_date=report_date,
        notification_chat_id=NOTIFY_CHAT,
    )


async def _set_cycle_clock(
    session: AsyncSession,
    cycle_id: int,
    *,
    last_activity_at: dt.datetime | None = None,
    reminder_count: int | None = None,
    last_reminder_at: dt.datetime | None = None,
    claim_token: uuid.UUID | None = None,
    claimed_at: dt.datetime | None = None,
    clear_claim: bool = False,
) -> None:
    async with session.begin():
        cycle = await session.get(AuditCycle, cycle_id)
        assert cycle is not None
        if last_activity_at is not None:
            cycle.last_activity_at = last_activity_at
        if reminder_count is not None:
            cycle.reminder_count = reminder_count
        if last_reminder_at is not None:
            cycle.last_reminder_at = last_reminder_at
        if clear_claim:
            cycle.reminder_claim_token = None
            cycle.reminder_claimed_at = None
        if claim_token is not None:
            cycle.reminder_claim_token = claim_token
            cycle.reminder_claimed_at = claimed_at or dt.datetime.now(UTC)


@pytest.mark.asyncio
async def test_first_and_repeat_reminder_and_missing_list(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    settings = _settings()
    sent: list[tuple[int, str]] = []

    async def fake_send(*, chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    async with stage3_session_maker() as session:
        add_result = await seed_collecting(session, valid_result, sha="rem-1")
        cycle_id = add_result.cycle_id
        await _set_cycle_clock(
            session,
            cycle_id,
            last_activity_at=dt.datetime.now(UTC) - dt.timedelta(hours=2),
        )

    scheduler = IdleReminderScheduler(
        bot=None,  # type: ignore[arg-type]
        session_maker=stage3_session_maker,
        settings=settings,  # type: ignore[arg-type]
        send_message=fake_send,
    )
    await scheduler.run_once(now_utc=dt.datetime.now(UTC))
    assert len(sent) == 1
    assert sent[0][0] == NOTIFY_CHAT
    assert "1/5" in sent[0][1]
    assert DEPARTMENT_LABELS[Department.MOSCOW] in sent[0][1]

    async with stage3_session_maker() as session, session.begin():
        cycle = await session.get(AuditCycle, cycle_id)
        assert cycle is not None
        assert cycle.reminder_count == 1
        assert cycle.last_reminder_at is not None
        assert cycle.reminder_claim_token is None
        count_after_first = cycle.reminder_count
        last_at = cycle.last_reminder_at

    # Premature: interval not elapsed
    await scheduler.run_once(now_utc=dt.datetime.now(UTC))
    assert len(sent) == 1

    async with stage3_session_maker() as session:
        await _set_cycle_clock(
            session,
            cycle_id,
            last_reminder_at=dt.datetime.now(UTC) - dt.timedelta(hours=2),
            reminder_count=count_after_first,
        )

    await scheduler.run_once(now_utc=dt.datetime.now(UTC))
    assert len(sent) == 2

    async with stage3_session_maker() as session, session.begin():
        cycle = await session.get(AuditCycle, cycle_id)
        assert cycle is not None
        assert cycle.reminder_count == 2
        assert cycle.last_reminder_at != last_at

    # missing matches /status formatter
    async with stage3_session_maker() as session:
        views = await list_cycle_statuses(session)
    collecting = next(v for v in views if v.id == cycle_id)
    status_text = format_cycle_block(collecting)
    reminder_text = format_reminder_message(
        report_date=collecting.report_date,
        present_count=len(collecting.summary.present),
        missing_labels=tuple(
            DEPARTMENT_LABELS[d]
            for d in sorted(collecting.summary.missing, key=lambda x: x.value)
        ),
    )
    for label in (
        DEPARTMENT_LABELS[d] for d in collecting.summary.missing
    ):
        assert label in status_text
        assert label in reminder_text


@pytest.mark.asyncio
async def test_no_premature_reminder(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    sent: list[object] = []

    async def fake_send(**kwargs: object) -> None:
        sent.append(kwargs)

    async with stage3_session_maker() as session:
        add_result = await seed_collecting(session, valid_result, sha="premature")
        # last_activity_at is "now" — should not remind

    scheduler = IdleReminderScheduler(
        bot=None,  # type: ignore[arg-type]
        session_maker=stage3_session_maker,
        settings=_settings(),  # type: ignore[arg-type]
        send_message=fake_send,
    )
    await scheduler.run_once(now_utc=dt.datetime.now(UTC))
    assert sent == []
    async with stage3_session_maker() as session, session.begin():
        cycle = await session.get(AuditCycle, add_result.cycle_id)
        assert cycle is not None
        assert cycle.reminder_count == 0


@pytest.mark.asyncio
async def test_parallel_claims_only_one_send(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    settings = _settings()
    async with stage3_session_maker() as session:
        add_result = await seed_collecting(session, valid_result, sha="claim-race")
        cycle_id = add_result.cycle_id
        await _set_cycle_clock(
            session,
            cycle_id,
            last_activity_at=dt.datetime.now(UTC) - dt.timedelta(hours=2),
        )

    async def try_claim():
        async with stage3_session_maker() as session:
            return await claim_reminder(
                session, cycle_id=cycle_id, settings=settings
            )

    first, second = await asyncio.gather(try_claim(), try_claim())
    winners = [c for c in (first, second) if c is not None]
    assert len(winners) == 1


@pytest.mark.asyncio
async def test_stale_claim_ttl_can_be_reclaimed(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    settings = _settings(audit_reminder_claim_ttl_seconds=60)
    async with stage3_session_maker() as session:
        add_result = await seed_collecting(session, valid_result, sha="ttl-claim")
        cycle_id = add_result.cycle_id
        await _set_cycle_clock(
            session,
            cycle_id,
            last_activity_at=dt.datetime.now(UTC) - dt.timedelta(hours=2),
            claim_token=uuid.uuid4(),
            claimed_at=dt.datetime.now(UTC) - dt.timedelta(seconds=120),
        )

    async with stage3_session_maker() as session:
        claim = await claim_reminder(session, cycle_id=cycle_id, settings=settings)
    assert claim is not None


@pytest.mark.asyncio
async def test_fresh_claim_blocks_second_worker(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    settings = _settings()
    async with stage3_session_maker() as session:
        add_result = await seed_collecting(session, valid_result, sha="fresh-claim")
        cycle_id = add_result.cycle_id
        await _set_cycle_clock(
            session,
            cycle_id,
            last_activity_at=dt.datetime.now(UTC) - dt.timedelta(hours=2),
            claim_token=uuid.uuid4(),
            claimed_at=dt.datetime.now(UTC),
        )

    async with stage3_session_maker() as session:
        claim = await claim_reminder(session, cycle_id=cycle_id, settings=settings)
    assert claim is None


@pytest.mark.asyncio
async def test_telegram_error_does_not_increment_count(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async def boom(**kwargs: object) -> None:
        raise TelegramNetworkError(method="sendMessage", message="net down")  # type: ignore[arg-type]

    async with stage3_session_maker() as session:
        add_result = await seed_collecting(session, valid_result, sha="tg-fail")
        cycle_id = add_result.cycle_id
        await _set_cycle_clock(
            session,
            cycle_id,
            last_activity_at=dt.datetime.now(UTC) - dt.timedelta(hours=2),
        )

    scheduler = IdleReminderScheduler(
        bot=None,  # type: ignore[arg-type]
        session_maker=stage3_session_maker,
        settings=_settings(audit_reminder_error_backoff_seconds=60),  # type: ignore[arg-type]
        send_message=boom,
    )
    await scheduler.run_once(now_utc=dt.datetime.now(UTC))

    async with stage3_session_maker() as session, session.begin():
        cycle = await session.get(AuditCycle, cycle_id)
        assert cycle is not None
        assert cycle.reminder_count == 0
        assert cycle.reminder_claim_token is None
        assert cycle.status == AuditCycleStatus.COLLECTING


@pytest.mark.asyncio
async def test_error_backoff_prevents_tight_retry(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    calls = {"n": 0}

    async def boom(**kwargs: object) -> None:
        calls["n"] += 1
        raise TelegramNetworkError(method="sendMessage", message="net down")  # type: ignore[arg-type]

    async with stage3_session_maker() as session:
        add_result = await seed_collecting(session, valid_result, sha="backoff")
        cycle_id = add_result.cycle_id
        await _set_cycle_clock(
            session,
            cycle_id,
            last_activity_at=dt.datetime.now(UTC) - dt.timedelta(hours=2),
        )

    scheduler = IdleReminderScheduler(
        bot=None,  # type: ignore[arg-type]
        session_maker=stage3_session_maker,
        settings=_settings(audit_reminder_error_backoff_seconds=3600),  # type: ignore[arg-type]
        send_message=boom,
    )
    now = dt.datetime.now(UTC)
    await scheduler.run_once(now_utc=now)
    await scheduler.run_once(now_utc=now + dt.timedelta(seconds=60))
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_activity_resets_series_and_claim_status_does_not(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async with stage3_session_maker() as session:
        add_result = await seed_collecting(session, valid_result, sha="reset-a")
        cycle_id = add_result.cycle_id
        token = uuid.uuid4()
        await _set_cycle_clock(
            session,
            cycle_id,
            reminder_count=2,
            last_reminder_at=dt.datetime.now(UTC) - dt.timedelta(days=1),
            claim_token=token,
            claimed_at=dt.datetime.now(UTC),
        )

    # /status must not reset
    async with stage3_session_maker() as session:
        before = await list_cycle_statuses(session)
        assert any(v.id == cycle_id for v in before)
    async with stage3_session_maker() as session, session.begin():
        cycle = await session.get(AuditCycle, cycle_id)
        assert cycle is not None
        assert cycle.reminder_count == 2
        assert cycle.reminder_claim_token == token

    # real activity resets
    async with stage3_session_maker() as session:
        dated = result_for_date(valid_result, REPORT_DATE)
        await add_source_file_atomic(
            session,
            result=dated,
            department=Department.MOSCOW,
            sha256="reset-b",
            original_filename="reset-b.xls",
            report_date=REPORT_DATE,
            notification_chat_id=NOTIFY_CHAT,
        )

    async with stage3_session_maker() as session, session.begin():
        cycle = await session.get(AuditCycle, cycle_id)
        assert cycle is not None
        assert cycle.reminder_count == 0
        assert cycle.last_reminder_at is None
        assert cycle.reminder_claim_token is None


@pytest.mark.asyncio
async def test_expire_after_max_successful_reminders_and_grace(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    settings = _settings(audit_max_reminders=2, audit_expire_grace_seconds=3600)
    async with stage3_session_maker() as session:
        add_result = await seed_collecting(session, valid_result, sha="expire-1")
        cycle_id = add_result.cycle_id
        await _set_cycle_clock(
            session,
            cycle_id,
            reminder_count=2,
            last_reminder_at=dt.datetime.now(UTC) - dt.timedelta(hours=2),
            last_activity_at=dt.datetime.now(UTC) - dt.timedelta(days=3),
            clear_claim=True,
        )

    async with stage3_session_maker() as session:
        expired = await expire_cycle_if_due(
            session, cycle_id=cycle_id, settings=settings
        )
    assert expired is True

    async with stage3_session_maker() as session, session.begin():
        cycle = await session.get(AuditCycle, cycle_id)
        assert cycle is not None
        assert cycle.status == AuditCycleStatus.EXPIRED
        assert cycle.expired_at is not None
        assert cycle.status != AuditCycleStatus.COMPLETED


@pytest.mark.asyncio
async def test_completed_and_expired_ignored_by_scheduler(
    stage3_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    sent: list[object] = []

    async def fake_send(**kwargs: object) -> None:
        sent.append(kwargs)

    async with stage3_session_maker() as session, session.begin():
        session.add_all(
            [
                AuditCycle(
                    notification_chat_id=NOTIFY_CHAT,
                    report_date=REPORT_DATE,
                    status=AuditCycleStatus.COMPLETED,
                    last_activity_at=dt.datetime.now(UTC) - dt.timedelta(days=10),
                    reminder_count=0,
                ),
                AuditCycle(
                    notification_chat_id=NOTIFY_CHAT,
                    report_date=REPORT_DATE + dt.timedelta(days=1),
                    status=AuditCycleStatus.EXPIRED,
                    last_activity_at=dt.datetime.now(UTC) - dt.timedelta(days=10),
                    reminder_count=0,
                ),
            ]
        )

    scheduler = IdleReminderScheduler(
        bot=None,  # type: ignore[arg-type]
        session_maker=stage3_session_maker,
        settings=_settings(),  # type: ignore[arg-type]
        send_message=fake_send,
    )
    await scheduler.run_once(now_utc=dt.datetime.now(UTC))
    assert sent == []


@pytest.mark.asyncio
async def test_race_expire_vs_activity_serialized_outcomes(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    settings = _settings(audit_max_reminders=2, audit_expire_grace_seconds=0)
    async with stage3_session_maker() as session:
        add_result = await seed_collecting(session, valid_result, sha="race-exp")
        cycle_id = add_result.cycle_id
        await _set_cycle_clock(
            session,
            cycle_id,
            reminder_count=2,
            last_reminder_at=dt.datetime.now(UTC) - dt.timedelta(days=1),
            last_activity_at=dt.datetime.now(UTC) - dt.timedelta(days=3),
            clear_claim=True,
        )

    barrier = asyncio.Barrier(2)
    outcomes: dict[str, object] = {}

    async def do_expire() -> None:
        await barrier.wait()
        async with stage3_session_maker() as session:
            outcomes["expire"] = await expire_cycle_if_due(
                session, cycle_id=cycle_id, settings=settings
            )

    async def do_activity() -> None:
        await barrier.wait()
        dated = result_for_date(valid_result, REPORT_DATE)
        try:
            async with stage3_session_maker() as session:
                await add_source_file_atomic(
                    session,
                    result=dated,
                    department=Department.MOSCOW,
                    sha256="race-exp-act",
                    original_filename="race-exp-act.xls",
                    report_date=REPORT_DATE,
                    notification_chat_id=NOTIFY_CHAT,
                )
            outcomes["activity"] = "ok"
        except CycleImmutableError:
            outcomes["activity"] = "immutable"

    await asyncio.gather(do_expire(), do_activity())

    async with stage3_session_maker() as session, session.begin():
        cycle = await session.get(AuditCycle, cycle_id)
        assert cycle is not None
        files = (
            await session.scalars(
                select(AuditCycle).where(AuditCycle.id == cycle_id)
            )
        )
        _ = files
        from app.domain.models import SourceFile

        active = (
            await session.scalars(
                select(SourceFile).where(
                    SourceFile.audit_cycle_id == cycle_id,
                    SourceFile.lifecycle_status == SourceFileLifecycle.ACTIVE,
                )
            )
        ).all()

        # Two legal serialized outcomes only.
        if cycle.status == AuditCycleStatus.COLLECTING:
            assert outcomes["expire"] is False
            assert outcomes["activity"] == "ok"
            assert cycle.reminder_count == 0
            assert len(active) == 2
        elif cycle.status == AuditCycleStatus.EXPIRED:
            assert outcomes["expire"] is True
            assert outcomes["activity"] == "immutable"
            assert cycle.expired_at is not None
            # No new active file from rejected activity — still the seed one.
            assert len(active) == 1
        else:
            raise AssertionError(f"unexpected status {cycle.status}")


@pytest.mark.asyncio
async def test_scheduler_restart_continues_from_postgres(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    sent: list[str] = []

    async def fake_send(*, chat_id: int, text: str) -> None:
        sent.append(text)

    async with stage3_session_maker() as session:
        add_result = await seed_collecting(session, valid_result, sha="restart")
        cycle_id = add_result.cycle_id
        await _set_cycle_clock(
            session,
            cycle_id,
            reminder_count=1,
            last_reminder_at=dt.datetime.now(UTC) - dt.timedelta(hours=2),
            last_activity_at=dt.datetime.now(UTC) - dt.timedelta(days=2),
            clear_claim=True,
        )

    # New scheduler instance (simulates process restart) — no MemoryStorage state.
    scheduler = IdleReminderScheduler(
        bot=None,  # type: ignore[arg-type]
        session_maker=stage3_session_maker,
        settings=_settings(),  # type: ignore[arg-type]
        send_message=fake_send,
    )
    await scheduler.run_once(now_utc=dt.datetime.now(UTC))
    assert len(sent) == 1
    async with stage3_session_maker() as session, session.begin():
        cycle = await session.get(AuditCycle, cycle_id)
        assert cycle is not None
        assert cycle.reminder_count == 2


@pytest.mark.asyncio
async def test_graceful_shutdown_and_per_cycle_isolation(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    sent: list[int] = []

    async def selective_send(*, chat_id: int, text: str) -> None:
        if "08.01.2026" in text or "01.08.2026" in text:
            # first cycle date formatting DD.MM.YYYY
            pass
        if "bad" in text:
            raise RuntimeError("boom")
        sent.append(chat_id)

    # Two collecting cycles
    async with stage3_session_maker() as session:
        a = await seed_collecting(
            session, valid_result, sha="iso-a", report_date=REPORT_DATE
        )
        b = await seed_collecting(
            session,
            valid_result,
            sha="iso-b",
            report_date=REPORT_DATE + dt.timedelta(days=1),
        )
        for cycle_id in (a.cycle_id, b.cycle_id):
            await _set_cycle_clock(
                session,
                cycle_id,
                last_activity_at=dt.datetime.now(UTC) - dt.timedelta(hours=2),
            )

    # Force format path: use real formatter — both should succeed.
    # Isolation: monkeypatch process to raise on first id.
    scheduler = IdleReminderScheduler(
        bot=None,  # type: ignore[arg-type]
        session_maker=stage3_session_maker,
        settings=_settings(),  # type: ignore[arg-type]
        send_message=selective_send,
    )

    original = scheduler._process_cycle
    seen = {"n": 0}

    async def flaky_process(cycle_id: int, *, now_utc: dt.datetime) -> None:
        seen["n"] += 1
        if seen["n"] == 1:
            raise RuntimeError("isolated failure")
        await original(cycle_id, now_utc=now_utc)

    scheduler._process_cycle = flaky_process  # type: ignore[method-assign]
    await scheduler.run_once(now_utc=dt.datetime.now(UTC))
    assert seen["n"] == 2
    assert len(sent) == 1

    await scheduler.start()
    assert scheduler.running
    await scheduler.stop(timeout=2.0)
    assert not scheduler.running


@pytest.mark.asyncio
async def test_reminders_only_to_notification_chat_not_all_allowlisted(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    chats: list[int] = []

    async def fake_send(*, chat_id: int, text: str) -> None:
        chats.append(chat_id)

    async with stage3_session_maker() as session:
        add_result = await seed_collecting(session, valid_result, sha="notify-only")
        await _set_cycle_clock(
            session,
            add_result.cycle_id,
            last_activity_at=dt.datetime.now(UTC) - dt.timedelta(hours=2),
        )
        # Second cycle explicitly same notify chat (not OTHER_USER)
        add2 = await seed_collecting(
            session,
            valid_result,
            sha="notify-only-2",
            report_date=REPORT_DATE + dt.timedelta(days=2),
        )
        await _set_cycle_clock(
            session,
            add2.cycle_id,
            last_activity_at=dt.datetime.now(UTC) - dt.timedelta(hours=2),
        )

    scheduler = IdleReminderScheduler(
        bot=None,  # type: ignore[arg-type]
        session_maker=stage3_session_maker,
        settings=_settings(),  # type: ignore[arg-type]
        send_message=fake_send,
    )
    await scheduler.run_once(now_utc=dt.datetime.now(UTC))
    assert chats
    assert set(chats) == {NOTIFY_CHAT}
    assert OTHER_USER not in chats


@pytest.mark.asyncio
async def test_record_noop_when_claim_cleared_by_activity(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    settings = _settings()
    async with stage3_session_maker() as session:
        add_result = await seed_collecting(session, valid_result, sha="stale-rec")
        cycle_id = add_result.cycle_id
        await _set_cycle_clock(
            session,
            cycle_id,
            last_activity_at=dt.datetime.now(UTC) - dt.timedelta(hours=2),
        )
        claim = await claim_reminder(session, cycle_id=cycle_id, settings=settings)
        assert claim is not None

    # Activity clears claim
    async with stage3_session_maker() as session:
        dated = result_for_date(valid_result, REPORT_DATE)
        await add_source_file_atomic(
            session,
            result=dated,
            department=Department.SZFO_1,
            sha256="stale-rec-act",
            original_filename="x.xls",
            report_date=REPORT_DATE,
            notification_chat_id=NOTIFY_CHAT,
        )

    async with stage3_session_maker() as session:
        recorded = await record_successful_reminder(
            session,
            cycle_id=cycle_id,
            claim_token=claim.claim_token,
            expected_reminder_count=claim.reminder_count,
        )
    assert recorded is False
    async with stage3_session_maker() as session, session.begin():
        cycle = await session.get(AuditCycle, cycle_id)
        assert cycle is not None
        assert cycle.reminder_count == 0


@pytest.mark.asyncio
async def test_release_claim_on_failure_keeps_count(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    settings = _settings()
    async with stage3_session_maker() as session:
        add_result = await seed_collecting(session, valid_result, sha="rel-claim")
        cycle_id = add_result.cycle_id
        await _set_cycle_clock(
            session,
            cycle_id,
            last_activity_at=dt.datetime.now(UTC) - dt.timedelta(hours=2),
        )
        claim = await claim_reminder(session, cycle_id=cycle_id, settings=settings)
        assert claim is not None
        released = await release_reminder_claim(
            session, cycle_id=cycle_id, claim_token=claim.claim_token
        )
        assert released is True

    async with stage3_session_maker() as session, session.begin():
        cycle = await session.get(AuditCycle, cycle_id)
        assert cycle is not None
        assert cycle.reminder_count == 0
        assert cycle.reminder_claim_token is None
