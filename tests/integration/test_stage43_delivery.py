"""Stage 4.3 Telegram delivery + /report integration tests."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import BufferedInputFile, Chat, Message, User
from aiogram.filters.command import CommandObject
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.audit_service import add_source_file_atomic
from app.application.report_delivery_service import (
    claim_delivery,
    complete_delivery,
    create_manual_delivery,
    fail_delivery,
    list_due_delivery_ids,
    record_document_sent,
    record_summary_message_sent,
    recover_missing_automatic_deliveries,
    recover_stale_claimed_deliveries,
    resolve_report_artifact,
)
from app.application.report_service import (
    claim_report_build,
    complete_report_build,
    run_claimed_build,
)
from app.bot.handlers.report import handle_report
from app.bot.scheduler.report_scheduler import ReportScheduler
from app.domain.enums import Department
from app.domain.models import (
    AuditArtifact,
    AuditArtifactKind,
    AuditCycleStatus,
    AuditReport,
    AuditReportStatus,
    ReportDelivery,
    ReportDeliveryChannel,
    ReportDeliveryKind,
    ReportDeliveryStatus,
)
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
NOTIFY_CHAT_ID = 743971617
MANUAL_CHAT_ID = 111222333
USER_ID = 6840100810


@pytest.fixture
def valid_result() -> ValidationResult:
    result = validate_confirmed_template_file(FIXTURE)
    assert result.is_valid and result.parsed is not None
    return result


def result_for_date(result: ValidationResult, report_date: dt.date) -> ValidationResult:
    assert result.parsed is not None
    return replace(result, parsed=replace(result.parsed, report_date=report_date))


def _settings(**overrides: object) -> SimpleNamespace:
    base = {
        "report_build_claim_ttl_seconds": 300,
        "report_build_max_attempts": 5,
        "report_build_backoff_seconds": 1,
        "report_scheduler_poll_seconds": 30,
        "report_delivery_claim_ttl_seconds": 300,
        "report_delivery_send_timeout_seconds": 30,
        "report_delivery_max_attempts": 5,
        "report_delivery_backoff_seconds": 1,
        "report_delivery_max_file_bytes": 52428800,
        "report_delivery_batch_size": 10,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def complete_cycle(
    session: AsyncSession,
    result: ValidationResult,
    *,
    report_date: dt.date,
    sha_prefix: str,
) -> int:
    dated = result_for_date(result, report_date)
    last = None
    for index, department in enumerate(Department):
        last = await add_source_file_atomic(
            session,
            result=dated,
            department=department,
            sha256=f"{sha_prefix}-{index}",
            original_filename=f"{sha_prefix}-{index}.xls",
            report_date=report_date,
            notification_chat_id=NOTIFY_CHAT_ID,
        )
    assert last is not None
    assert last.status == AuditCycleStatus.COMPLETED
    return last.cycle_id


async def build_ready_core(
    maker: async_sessionmaker[AsyncSession],
    *,
    cycle_id: int,
) -> tuple[int, int, object]:
    async with maker() as session:
        report = await session.scalar(
            select(AuditReport).where(AuditReport.audit_cycle_id == cycle_id)
        )
        assert report is not None
        report_id = report.id
        await session.commit()
    settings = _settings()
    async with maker() as session:
        claim = await claim_report_build(
            session, report_id=report_id, settings=settings
        )
    assert claim is not None
    async with maker() as session:
        result = await run_claimed_build(session, claim=claim)
    async with maker() as session:
        ok = await complete_report_build(
            session,
            report_id=report_id,
            claim_token=claim.claim_token,
            summary_json=result.summary_json,
            excel_bytes=result.excel_bytes,
            excel_sha256=result.excel_sha256,
        )
        assert ok is True
    async with maker() as session:
        artifact = await session.scalar(
            select(AuditArtifact).where(
                AuditArtifact.audit_report_id == report_id,
                AuditArtifact.kind == AuditArtifactKind.CORE,
            )
        )
        assert artifact is not None
        delivery = await session.scalar(
            select(ReportDelivery).where(
                ReportDelivery.audit_artifact_id == artifact.id,
                ReportDelivery.kind == ReportDeliveryKind.AUTOMATIC,
            )
        )
        assert delivery is not None
        return report_id, artifact.id, result


@pytest.mark.asyncio
async def test_complete_build_enqueues_automatic_same_txn(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async with stage3_session_maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 10, 1),
            sha_prefix="s43-atom",
        )
        await session.commit()
    report_id, artifact_id, _ = await build_ready_core(
        stage3_session_maker, cycle_id=cycle_id
    )
    async with stage3_session_maker() as session:
        report = await session.get(AuditReport, report_id)
        assert report is not None
        assert report.status == AuditReportStatus.READY
        delivery = await session.scalar(
            select(ReportDelivery).where(
                ReportDelivery.audit_artifact_id == artifact_id,
                ReportDelivery.kind == ReportDeliveryKind.AUTOMATIC,
            )
        )
        assert delivery is not None
        assert delivery.status == ReportDeliveryStatus.PENDING
        assert delivery.destination_chat_id == NOTIFY_CHAT_ID


@pytest.mark.asyncio
async def test_enqueue_failure_rolls_back_core_ready(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    from sqlalchemy import event

    from app.domain.models import ReportDelivery as RD

    async with stage3_session_maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 10, 2),
            sha_prefix="s43-roll",
        )
        report = await session.scalar(
            select(AuditReport).where(AuditReport.audit_cycle_id == cycle_id)
        )
        assert report is not None
        report_id = report.id
        await session.commit()

    settings = _settings()
    async with stage3_session_maker() as session:
        claim = await claim_report_build(
            session, report_id=report_id, settings=settings
        )
    assert claim is not None
    async with stage3_session_maker() as session:
        result = await run_claimed_build(session, claim=claim)

    def _boom(mapper, connection, target) -> None:  # noqa: ARG001
        raise RuntimeError("forced delivery enqueue failure")

    event.listen(RD, "before_insert", _boom)
    try:
        with pytest.raises(RuntimeError, match="forced delivery enqueue failure"):
            async with stage3_session_maker() as session:
                await complete_report_build(
                    session,
                    report_id=report_id,
                    claim_token=claim.claim_token,
                    summary_json=result.summary_json,
                    excel_bytes=result.excel_bytes,
                    excel_sha256=result.excel_sha256,
                )
    finally:
        event.remove(RD, "before_insert", _boom)

    async with stage3_session_maker() as session:
        report = await session.get(AuditReport, report_id)
        assert report is not None
        assert report.status == AuditReportStatus.BUILDING
        assert report.build_claim_token == claim.claim_token
        assert report.summary_json is None
        assert (
            await session.scalar(
                select(AuditArtifact.id).where(
                    AuditArtifact.audit_report_id == report_id
                )
            )
            is None
        )
        assert await session.scalar(select(func.count()).select_from(ReportDelivery)) == 0


@pytest.mark.asyncio
async def test_recovery_and_unique_automatic(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async with stage3_session_maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 10, 3),
            sha_prefix="s43-rec",
        )
        await session.commit()
    _, artifact_id, _ = await build_ready_core(
        stage3_session_maker, cycle_id=cycle_id
    )
    async with stage3_session_maker() as session:
        delivery = await session.scalar(
            select(ReportDelivery).where(
                ReportDelivery.audit_artifact_id == artifact_id
            )
        )
        assert delivery is not None
        await session.delete(delivery)
        await session.commit()

    async with stage3_session_maker() as session:
        created = await recover_missing_automatic_deliveries(session)
    assert len(created) == 1
    async with stage3_session_maker() as session:
        created2 = await recover_missing_automatic_deliveries(session)
    assert created2 == []


@pytest.mark.asyncio
async def test_scheduler_delivers_once_to_notification_chat(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async with stage3_session_maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 10, 4),
            sha_prefix="s43-sched",
        )
        await session.commit()

    sent_messages: list[tuple[int, str]] = []
    sent_docs: list[int] = []
    msg_id = 1000

    async def fake_send_message(*, chat_id: int, text: str, **kwargs: object) -> object:
        nonlocal msg_id
        sent_messages.append((chat_id, text))
        msg_id += 1
        return SimpleNamespace(message_id=msg_id)

    async def fake_send_document(*, chat_id: int, document: object, **kwargs: object) -> object:
        nonlocal msg_id
        sent_docs.append(chat_id)
        assert isinstance(document, BufferedInputFile) or hasattr(document, "filename")
        msg_id += 1
        return SimpleNamespace(message_id=msg_id)

    bot = AsyncMock()
    scheduler = ReportScheduler(
        bot=bot,
        session_maker=stage3_session_maker,
        settings=_settings(),  # type: ignore[arg-type]
        send_message=fake_send_message,
        send_document=fake_send_document,
    )
    await scheduler.run_once()
    assert sent_messages
    assert all(chat == NOTIFY_CHAT_ID for chat, _ in sent_messages)
    assert sent_docs == [NOTIFY_CHAT_ID]
    # summary before document
    assert sent_messages[0][0] == NOTIFY_CHAT_ID

    sent_before = len(sent_docs)
    await scheduler.run_once()
    assert len(sent_docs) == sent_before

    async with stage3_session_maker() as session:
        deliveries = (
            await session.execute(
                select(ReportDelivery).where(
                    ReportDelivery.kind == ReportDeliveryKind.AUTOMATIC,
                    ReportDelivery.status == ReportDeliveryStatus.DELIVERED,
                )
            )
        ).scalars().all()
        assert len(deliveries) == 1


@pytest.mark.asyncio
async def test_manual_resend_and_claim_fencing(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async with stage3_session_maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 10, 5),
            sha_prefix="s43-man",
        )
        await session.commit()
    _, artifact_id, result = await build_ready_core(
        stage3_session_maker, cycle_id=cycle_id
    )
    settings = _settings()

    async with stage3_session_maker() as session:
        d1_id = await create_manual_delivery(
            session,
            artifact_id=artifact_id,
            destination_chat_id=MANUAL_CHAT_ID,
            requested_by_user_id=USER_ID,
        )
        d2_id = await create_manual_delivery(
            session,
            artifact_id=artifact_id,
            destination_chat_id=MANUAL_CHAT_ID,
            requested_by_user_id=USER_ID,
        )
        assert d1_id != d2_id
        d1 = await session.get(ReportDelivery, d1_id)
        assert d1 is not None
        assert d1.requested_by_user_id == USER_ID
        assert d1.destination_chat_id == MANUAL_CHAT_ID

    async with stage3_session_maker() as session:
        claim_a = await claim_delivery(
            session, delivery_id=d1_id, settings=settings
        )
    assert claim_a is not None
    token_a = claim_a.claim_token

    # Expire claim and reclaim with token B
    async with stage3_session_maker() as session:
        delivery = await session.get(ReportDelivery, d1_id)
        assert delivery is not None
        delivery.claimed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            seconds=400
        )
        await session.commit()
    async with stage3_session_maker() as session:
        recovered = await recover_stale_claimed_deliveries(
            session, settings=settings
        )
    assert d1_id in recovered
    async with stage3_session_maker() as session:
        claim_b = await claim_delivery(
            session, delivery_id=d1_id, settings=settings
        )
    assert claim_b is not None
    token_b = claim_b.claim_token
    assert token_a != token_b

    async with stage3_session_maker() as session:
        assert (
            await record_summary_message_sent(
                session,
                delivery_id=d1_id,
                claim_token=token_a,
                message_id=1,
            )
            is False
        )
        assert (
            await record_document_sent(
                session,
                delivery_id=d1_id,
                claim_token=token_a,
                message_id=2,
            )
            is False
        )
        assert (
            await complete_delivery(
                session, delivery_id=d1_id, claim_token=token_a
            )
            is False
        )
        assert (
            await fail_delivery(
                session,
                delivery_id=d1_id,
                claim_token=token_a,
                error="x",
                settings=settings,
            )
            is False
        )
        assert (
            await record_summary_message_sent(
                session,
                delivery_id=d1_id,
                claim_token=token_b,
                message_id=10,
            )
            is True
        )
        assert (
            await record_document_sent(
                session,
                delivery_id=d1_id,
                claim_token=token_b,
                message_id=11,
            )
            is True
        )
        assert (
            await complete_delivery(
                session, delivery_id=d1_id, claim_token=token_b
            )
            is True
        )


@pytest.mark.asyncio
async def test_document_already_sent_skips_resend(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async with stage3_session_maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 10, 6),
            sha_prefix="s43-doc",
        )
        await session.commit()
    _, artifact_id, _ = await build_ready_core(
        stage3_session_maker, cycle_id=cycle_id
    )
    settings = _settings()
    async with stage3_session_maker() as session:
        delivery = await session.scalar(
            select(ReportDelivery).where(
                ReportDelivery.audit_artifact_id == artifact_id,
                ReportDelivery.kind == ReportDeliveryKind.AUTOMATIC,
            )
        )
        assert delivery is not None
        delivery_id = delivery.id

    async with stage3_session_maker() as session:
        claim = await claim_delivery(
            session, delivery_id=delivery_id, settings=settings
        )
    assert claim is not None
    async with stage3_session_maker() as session:
        await record_document_sent(
            session,
            delivery_id=delivery_id,
            claim_token=claim.claim_token,
            message_id=4242,
        )
        # Crash before complete: force FAILED with document_message_id kept
        delivery = await session.get(ReportDelivery, delivery_id)
        assert delivery is not None
        delivery.status = ReportDeliveryStatus.FAILED
        delivery.claim_token = None
        delivery.claimed_at = None
        delivery.next_retry_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            seconds=1
        )
        await session.commit()

    send_document = AsyncMock()
    send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    bot = AsyncMock()
    scheduler = ReportScheduler(
        bot=bot,
        session_maker=stage3_session_maker,
        settings=settings,  # type: ignore[arg-type]
        send_message=send_message,
        send_document=send_document,
    )
    await scheduler.run_once()
    send_document.assert_not_called()
    async with stage3_session_maker() as session:
        delivery = await session.get(ReportDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == ReportDeliveryStatus.DELIVERED
        assert delivery.document_message_id == 4242


@pytest.mark.asyncio
async def test_resolve_states_and_core_force(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async with stage3_session_maker() as session:
        missing = await resolve_report_artifact(
            session, report_date=dt.date(2099, 1, 1)
        )
        assert missing.status.value == "cycle_not_found"

        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 10, 7),
            sha_prefix="s43-res",
        )
        await session.commit()

    async with stage3_session_maker() as session:
        pending = await resolve_report_artifact(
            session, report_date=dt.date(2026, 10, 7)
        )
        assert pending.status.value == "report_pending"

    await build_ready_core(stage3_session_maker, cycle_id=cycle_id)
    async with stage3_session_maker() as session:
        ready = await resolve_report_artifact(
            session, report_date=dt.date(2026, 10, 7)
        )
        assert ready.ready
        assert ready.artifact_id is not None
        assert ready.artifact_kind == AuditArtifactKind.CORE
        report = await session.scalar(
            select(AuditReport).where(AuditReport.audit_cycle_id == cycle_id)
        )
        assert report is not None
        session.add(
            AuditArtifact(
                audit_report_id=report.id,
                kind=AuditArtifactKind.ENRICHED,
                revision=2,
                excel_bytes=b"PK\x03\x04enriched",
                excel_sha256=hashlib.sha256(b"PK\x03\x04enriched").hexdigest(),
                financial_input_hash=report.input_hash,
                generator_version="x",
                schema_version="x",
            )
        )
        await session.commit()

    async with stage3_session_maker() as session:
        prefer = await resolve_report_artifact(
            session, report_date=dt.date(2026, 10, 7)
        )
        assert prefer.artifact_kind == AuditArtifactKind.ENRICHED
        assert prefer.artifact_revision == 2
        forced = await resolve_report_artifact(
            session, report_date=dt.date(2026, 10, 7), force_core=True
        )
        assert forced.artifact_kind == AuditArtifactKind.CORE
        assert forced.artifact_revision == 1


@pytest.mark.asyncio
async def test_partial_summary_resume_skips_already_sent(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    async with stage3_session_maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 10, 8),
            sha_prefix="s43-part",
        )
        await session.commit()
    _, artifact_id, _ = await build_ready_core(
        stage3_session_maker, cycle_id=cycle_id
    )
    settings = _settings()
    async with stage3_session_maker() as session:
        delivery = await session.scalar(
            select(ReportDelivery).where(
                ReportDelivery.audit_artifact_id == artifact_id,
                ReportDelivery.kind == ReportDeliveryKind.AUTOMATIC,
            )
        )
        assert delivery is not None
        delivery_id = delivery.id

    async with stage3_session_maker() as session:
        claim = await claim_delivery(
            session, delivery_id=delivery_id, settings=settings
        )
    assert claim is not None
    async with stage3_session_maker() as session:
        assert await record_summary_message_sent(
            session,
            delivery_id=delivery_id,
            claim_token=claim.claim_token,
            message_id=501,
        )
        delivery = await session.get(ReportDelivery, delivery_id)
        assert delivery is not None
        delivery.status = ReportDeliveryStatus.FAILED
        delivery.claim_token = None
        delivery.claimed_at = None
        delivery.next_retry_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            seconds=1
        )
        await session.commit()

    sent_messages: list[str] = []

    async def fake_send_message(*, chat_id: int, text: str, **kwargs: object) -> object:
        sent_messages.append(text)
        return SimpleNamespace(message_id=600 + len(sent_messages))

    async def fake_send_document(*, chat_id: int, document: object, **kwargs: object) -> object:
        return SimpleNamespace(message_id=700)

    scheduler = ReportScheduler(
        bot=AsyncMock(),
        session_maker=stage3_session_maker,
        settings=settings,  # type: ignore[arg-type]
        send_message=fake_send_message,
        send_document=fake_send_document,
    )
    await scheduler.run_once()
    # First summary chunk already recorded → resume from index 1 (may be empty).
    assert all("placeholder-never" not in m for m in sent_messages)
    async with stage3_session_maker() as session:
        delivery = await session.get(ReportDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == ReportDeliveryStatus.DELIVERED
        assert delivery.summary_sent_count >= 1
        assert 501 in (delivery.summary_message_ids or [])
        assert delivery.document_message_id == 700


@pytest.mark.asyncio
async def test_list_due_delivery_ids_skips_terminal_and_not_due_before_limit(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
) -> None:
    batch_size = 10
    max_attempts = 5
    settings = _settings(
        report_delivery_batch_size=batch_size,
        report_delivery_max_attempts=max_attempts,
    )
    now = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)

    async with stage3_session_maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 10, 10),
            sha_prefix="s43-due",
        )
        await session.commit()
    _, artifact_id, _ = await build_ready_core(
        stage3_session_maker, cycle_id=cycle_id
    )

    async with stage3_session_maker() as session:
        auto = await session.scalar(
            select(ReportDelivery).where(
                ReportDelivery.audit_artifact_id == artifact_id,
                ReportDelivery.kind == ReportDeliveryKind.AUTOMATIC,
            )
        )
        assert auto is not None
        auto.status = ReportDeliveryStatus.DELIVERED
        auto.delivered_at = now
        auto.document_message_id = 1
        blocked_ids: list[int] = []
        for index in range(batch_size):
            terminal = index % 2 == 0
            blocked = ReportDelivery(
                audit_artifact_id=artifact_id,
                channel=ReportDeliveryChannel.TELEGRAM,
                kind=ReportDeliveryKind.MANUAL,
                status=ReportDeliveryStatus.FAILED,
                destination_chat_id=MANUAL_CHAT_ID,
                requested_by_user_id=USER_ID,
                attempt_count=max_attempts if terminal else 1,
                next_retry_at=(
                    None if terminal else now + dt.timedelta(hours=1)
                ),
            )
            session.add(blocked)
            await session.flush()
            blocked_ids.append(blocked.id)
        await session.commit()

    async with stage3_session_maker() as session:
        pending_id = await create_manual_delivery(
            session,
            artifact_id=artifact_id,
            destination_chat_id=MANUAL_CHAT_ID,
            requested_by_user_id=USER_ID,
        )
        due_failed = ReportDelivery(
            audit_artifact_id=artifact_id,
            channel=ReportDeliveryChannel.TELEGRAM,
            kind=ReportDeliveryKind.MANUAL,
            status=ReportDeliveryStatus.FAILED,
            destination_chat_id=MANUAL_CHAT_ID,
            requested_by_user_id=USER_ID,
            attempt_count=1,
            next_retry_at=now - dt.timedelta(minutes=1),
        )
        session.add(due_failed)
        await session.flush()
        due_failed_id = due_failed.id
        await session.commit()

    async with stage3_session_maker() as session:
        due_ids = await list_due_delivery_ids(
            session, settings=settings, now_utc=now
        )

    assert due_ids == [pending_id, due_failed_id]
    assert pending_id > max(blocked_ids)
    assert due_failed_id > max(blocked_ids)
    assert not any(blocked_id in due_ids for blocked_id in blocked_ids)


@pytest.mark.asyncio
async def test_manual_report_send_timeout_fails_delivery_and_notifies_retry(
    stage3_session_maker: async_sessionmaker[AsyncSession],
    valid_result: ValidationResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with stage3_session_maker() as session:
        cycle_id = await complete_cycle(
            session,
            valid_result,
            report_date=dt.date(2026, 10, 11),
            sha_prefix="s43-timeout",
        )
        await session.commit()
    await build_ready_core(stage3_session_maker, cycle_id=cycle_id)

    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "report_delivery_send_timeout_seconds", 0.05)

    hang = asyncio.Event()

    async def hang_send_message(**kwargs: object) -> object:
        await hang.wait()
        return SimpleNamespace(message_id=1)

    bot = AsyncMock()
    bot.send_message = hang_send_message
    bot.send_document = AsyncMock()

    chat = Chat(id=MANUAL_CHAT_ID, type="private")
    user = User(id=USER_ID, is_bot=False, first_name="Test")
    message = AsyncMock(spec=Message)
    message.chat = chat
    message.from_user = user
    message.answer = AsyncMock()
    command = CommandObject(command="report", args="")

    async with stage3_session_maker() as session:
        await handle_report(message, command, session, bot)

    message.answer.assert_awaited()
    reply = message.answer.await_args.args[0]
    assert "автоматически" in reply.lower()

    async with stage3_session_maker() as session:
        delivery = await session.scalar(
            select(ReportDelivery)
            .where(
                ReportDelivery.destination_chat_id == MANUAL_CHAT_ID,
                ReportDelivery.kind == ReportDeliveryKind.MANUAL,
            )
            .order_by(ReportDelivery.id.desc())
        )
        assert delivery is not None
        assert delivery.status == ReportDeliveryStatus.FAILED
        assert delivery.attempt_count == 1


@pytest.mark.asyncio
async def test_report_scheduler_start_stop_restart() -> None:
    settings = _settings(report_scheduler_poll_seconds=60)
    scheduler = ReportScheduler(
        bot=AsyncMock(),
        session_maker=AsyncMock(),
        settings=settings,  # type: ignore[arg-type]
        send_message=AsyncMock(),
        send_document=AsyncMock(),
    )
    await scheduler.start()
    assert scheduler.running
    await scheduler.start()  # idempotent
    assert scheduler.running
    await scheduler.stop()
    assert not scheduler.running
    await scheduler.start()
    assert scheduler.running
    await scheduler.stop()
    assert not scheduler.running
