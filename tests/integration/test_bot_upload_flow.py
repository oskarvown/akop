from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import (
    add_source_file_atomic,
    find_audit_cycle_by_report_date,
    replace_source_file_atomic,
)
from app.bot.handlers.upload import (
    UploadStates,
    handle_department_callback,
    handle_document,
    handle_new_cycle_callback,
    handle_replace_callback,
)
from app.bot.handlers.status import handle_status
from app.domain.enums import Department
from app.domain.models import (
    AuditCycle,
    AuditCycleStatus,
    SourceFile,
    SourceFileLifecycle,
)
from app.infrastructure.excel.checksum import compute_sha256
from app.infrastructure.excel.validator import ValidationResult, validate_confirmed_template_file
from tests.fixtures.generate_regional_fixtures import _basic_spec
from tests.fixtures.regional_builder import build_regional_xls

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "regional"
VALID_FILE = FIXTURES / "regional_valid_basic.xls"
INVALID_FILE = FIXTURES / "regional_invalid_missing_columns.xlsx"


@dataclass
class FakeDocument:
    file_name: str
    payload: bytes

    @property
    def file_size(self) -> int:
        return len(self.payload)


class FakeMessage:
    def __init__(self, document: FakeDocument | None = None) -> None:
        self.document = document
        self.answers: list[tuple[str, Any]] = []

    async def answer(self, text: str, reply_markup: Any = None) -> None:
        self.answers.append((text, reply_markup))


class FakeCallback:
    def __init__(self, data: str, message: FakeMessage | None = None) -> None:
        self.data = data
        self.message = message or FakeMessage()
        self.answer_count = 0

    async def answer(self) -> None:
        self.answer_count += 1


class FakeBot:
    async def download(self, document: FakeDocument, destination: Path) -> None:
        destination.write_bytes(document.payload)


class FakeState:
    def __init__(self) -> None:
        self.state: str | None = None
        self.data: dict[str, Any] = {}

    async def get_state(self) -> str | None:
        return self.state

    async def set_state(self, state: Any) -> None:
        self.state = state.state if hasattr(state, "state") else state

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def set_data(self, data: dict[str, Any]) -> None:
        self.data = dict(data)

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def clear(self) -> None:
        self.state = None
        self.data = {}


def document_from(path: Path) -> FakeDocument:
    return FakeDocument(file_name=path.name, payload=path.read_bytes())


async def receive_valid(
    session: AsyncSession,
    state: FakeState,
    path: Path = VALID_FILE,
) -> FakeMessage:
    message = FakeMessage(document_from(path))
    await handle_document(message, state, FakeBot(), session)  # type: ignore[arg-type]
    return message


async def seed_file(
    session: AsyncSession,
    result: ValidationResult,
    department: Department,
    sha: str,
):
    assert result.parsed is not None
    return await add_source_file_atomic(
        session,
        result=result,
        department=department,
        sha256=sha,
        original_filename=f"{sha}.xls",
        report_date=result.parsed.report_date,
    )


@pytest.fixture
def valid_result() -> ValidationResult:
    result = validate_confirmed_template_file(VALID_FILE)
    assert result.is_valid and result.parsed is not None
    return result


@pytest.mark.asyncio
async def test_invalid_file_reports_reasons_and_keeps_database_empty(
    stage3_session: AsyncSession,
) -> None:
    state = FakeState()
    message = FakeMessage(document_from(INVALID_FILE))
    await handle_document(message, state, FakeBot(), stage3_session)  # type: ignore[arg-type]

    assert "Файл отклонён" in message.answers[-1][0]
    assert state.state is None
    async with stage3_session.begin():
        assert await stage3_session.scalar(select(func.count(SourceFile.id))) == 0


@pytest.mark.asyncio
async def test_department_selection_saves_new_file(
    stage3_session: AsyncSession,
) -> None:
    state = FakeState()
    await receive_valid(stage3_session, state)
    token = state.data["upload_token"]
    callback = FakeCallback(f"dept:{token}:{Department.REGIONAL.value}")
    await handle_department_callback(callback, state, stage3_session)  # type: ignore[arg-type]

    assert "1/5" in callback.message.answers[-1][0]
    assert callback.answer_count == 1
    assert state.state is None
    async with stage3_session.begin():
        assert await stage3_session.scalar(select(func.count(SourceFile.id))) == 1


@pytest.mark.asyncio
async def test_exact_duplicate_is_rejected_before_department_buttons(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    await seed_file(
        stage3_session,
        valid_result,
        Department.REGIONAL,
        compute_sha256(VALID_FILE),
    )
    state = FakeState()
    message = await receive_valid(stage3_session, state)

    assert "уже загружен" in message.answers[-1][0]
    assert state.state is None


@pytest.mark.asyncio
async def test_confirmed_replacement_supersedes_old_file(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    await seed_file(stage3_session, valid_result, Department.REGIONAL, "old-handler")
    state = FakeState()
    await receive_valid(stage3_session, state)
    token = state.data["upload_token"]
    choose = FakeCallback(f"dept:{token}:{Department.REGIONAL.value}")
    await handle_department_callback(choose, state, stage3_session)  # type: ignore[arg-type]
    assert state.state == UploadStates.confirming_replace.state

    confirm = FakeCallback(f"replace:{token}:confirm")
    await handle_replace_callback(confirm, state, stage3_session)  # type: ignore[arg-type]
    assert "1/5" in confirm.message.answers[-1][0]

    async with stage3_session.begin():
        lifecycles = (
            await stage3_session.scalars(
                select(SourceFile.lifecycle_status).order_by(SourceFile.id)
            )
        ).all()
        assert lifecycles == [
            SourceFileLifecycle.SUPERSEDED,
            SourceFileLifecycle.ACTIVE,
        ]


@pytest.mark.asyncio
async def test_cancel_replacement_keeps_old_file_active(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    await seed_file(stage3_session, valid_result, Department.REGIONAL, "old-cancel")
    state = FakeState()
    await receive_valid(stage3_session, state)
    token = state.data["upload_token"]
    await handle_department_callback(  # type: ignore[arg-type]
        FakeCallback(f"dept:{token}:{Department.REGIONAL.value}"),
        state,
        stage3_session,
    )
    cancel = FakeCallback(f"replace:{token}:cancel")
    await handle_replace_callback(cancel, state, stage3_session)  # type: ignore[arg-type]

    assert "Оставляю старый" in cancel.message.answers[-1][0]
    async with stage3_session.begin():
        files = (await stage3_session.scalars(select(SourceFile))).all()
        assert len(files) == 1
        assert files[0].lifecycle_status == SourceFileLifecycle.ACTIVE


@pytest.mark.asyncio
async def test_stale_upload_token_does_nothing(
    stage3_session: AsyncSession,
) -> None:
    state = FakeState()
    await receive_valid(stage3_session, state)
    callback = FakeCallback(f"dept:obsolete:{Department.REGIONAL.value}")
    await handle_department_callback(callback, state, stage3_session)  # type: ignore[arg-type]

    assert "устарела" in callback.message.answers[-1][0]
    assert callback.answer_count == 1
    assert state.state == UploadStates.choosing_department.state


@pytest.mark.asyncio
async def test_completed_cycle_is_blocked_by_handler(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    assert valid_result.parsed is not None
    async with stage3_session.begin():
        stage3_session.add(
            AuditCycle(
                report_date=valid_result.parsed.report_date,
                status=AuditCycleStatus.COMPLETED,
            )
        )

    state = FakeState()
    await receive_valid(stage3_session, state)
    token = state.data["upload_token"]
    callback = FakeCallback(f"dept:{token}:{Department.MOSCOW.value}")
    await handle_department_callback(callback, state, stage3_session)  # type: ignore[arg-type]
    assert "уже завершён" in callback.message.answers[-1][0]
    assert state.state is None


@pytest.mark.asyncio
async def test_second_date_requires_confirmation_before_cycle_creation(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
    tmp_path: Path,
) -> None:
    await seed_file(stage3_session, valid_result, Department.REGIONAL, "first-date")
    assert valid_result.parsed is not None
    second_path = tmp_path / "second-date.xls"
    second_date = valid_result.parsed.report_date.replace(day=18)
    build_regional_xls(_basic_spec(second_date.strftime("%d.%m.%Y")), second_path)

    state = FakeState()
    await receive_valid(stage3_session, state, second_path)
    token = state.data["upload_token"]
    choose = FakeCallback(f"dept:{token}:{Department.MOSCOW.value}")
    await handle_department_callback(choose, state, stage3_session)  # type: ignore[arg-type]
    assert state.state == UploadStates.confirming_new_cycle.state
    assert await find_audit_cycle_by_report_date(stage3_session, second_date) is None

    confirm = FakeCallback(f"newcycle:{token}:confirm")
    await handle_new_cycle_callback(confirm, state, stage3_session)  # type: ignore[arg-type]
    assert await find_audit_cycle_by_report_date(stage3_session, second_date) is not None


@pytest.mark.asyncio
async def test_stale_replacement_confirmation_does_not_overwrite_new_active(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    first = await seed_file(
        stage3_session, valid_result, Department.REGIONAL, "stale-old"
    )
    state = FakeState()
    await receive_valid(stage3_session, state)
    token = state.data["upload_token"]
    await handle_department_callback(  # type: ignore[arg-type]
        FakeCallback(f"dept:{token}:{Department.REGIONAL.value}"),
        state,
        stage3_session,
    )

    assert valid_result.parsed is not None
    await replace_source_file_atomic(
        stage3_session,
        result=valid_result,
        department=Department.REGIONAL,
        sha256="parallel-new",
        original_filename="parallel-new.xls",
        report_date=valid_result.parsed.report_date,
        expected_active_source_file_id=first.source_file_id,
    )
    stale = FakeCallback(f"replace:{token}:confirm")
    await handle_replace_callback(stale, state, stage3_session)  # type: ignore[arg-type]
    assert "уже изменился" in stale.message.answers[-1][0]

    async with stage3_session.begin():
        active_shas = (
            await stage3_session.scalars(
                select(SourceFile.sha256).where(
                    SourceFile.lifecycle_status == SourceFileLifecycle.ACTIVE
                )
            )
        ).all()
        assert active_shas == ["parallel-new"]


@pytest.mark.asyncio
async def test_legacy_duplicate_has_safe_message_without_cycle_access(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    await seed_file(
        stage3_session,
        valid_result,
        Department.REGIONAL,
        compute_sha256(VALID_FILE),
    )
    async with stage3_session.begin():
        source = await stage3_session.scalar(select(SourceFile))
        assert source is not None
        source.audit_cycle_id = None
        await stage3_session.flush()
        await stage3_session.execute(AuditCycle.__table__.delete())

    state = FakeState()
    message = await receive_valid(stage3_session, state)
    assert "не привязан к недельному циклу" in message.answers[-1][0]
    assert state.state is None


@pytest.mark.asyncio
async def test_status_reads_open_cycle_from_database(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    await seed_file(stage3_session, valid_result, Department.REGIONAL, "status-file")
    message = FakeMessage()

    await handle_status(message, stage3_session)  # type: ignore[arg-type]

    text = message.answers[-1][0]
    assert "1/5" in text
    assert "Не хватает" in text
    assert "Региональный" in text


@pytest.mark.asyncio
async def test_callback_token_rejected_after_state_cleared_like_bot_restart(
    stage3_session: AsyncSession,
) -> None:
    """MemoryStorage empties FSM on process restart; old inline buttons must no-op."""
    live_state = FakeState()
    await receive_valid(stage3_session, live_state)
    token = live_state.data["upload_token"]

    restarted_state = FakeState()  # empty FSM after MemoryStorage restart
    for callback_data, handler in (
        (f"dept:{token}:{Department.REGIONAL.value}", handle_department_callback),
        (f"newcycle:{token}:confirm", handle_new_cycle_callback),
        (f"replace:{token}:confirm", handle_replace_callback),
    ):
        callback = FakeCallback(callback_data)
        await handler(callback, restarted_state, stage3_session)  # type: ignore[arg-type]
        assert "устарела" in callback.message.answers[-1][0]
        assert callback.answer_count == 1

    async with stage3_session.begin():
        assert await stage3_session.scalar(select(func.count(SourceFile.id))) == 0


@pytest.mark.asyncio
async def test_status_shows_all_collecting_cycles_and_recent_completed(
    stage3_session: AsyncSession,
    valid_result: ValidationResult,
) -> None:
    assert valid_result.parsed is not None
    older = valid_result.parsed.report_date
    newer = older + dt.timedelta(days=7)
    completed_date = older - dt.timedelta(days=7)

    await seed_file(stage3_session, valid_result, Department.REGIONAL, "status-old")
    await add_source_file_atomic(
        stage3_session,
        result=replace(
            valid_result,
            parsed=replace(valid_result.parsed, report_date=newer),
        ),
        department=Department.MOSCOW,
        sha256="status-new",
        original_filename="status-new.xls",
        report_date=newer,
    )
    async with stage3_session.begin():
        stage3_session.add(
            AuditCycle(
                report_date=completed_date,
                status=AuditCycleStatus.COMPLETED,
                completed_at=dt.datetime.now(tz=dt.timezone.utc),
            )
        )

    message = FakeMessage()
    await handle_status(message, stage3_session)  # type: ignore[arg-type]
    text = message.answers[-1][0]

    assert f"Сбор за {newer:%d.%m.%Y}" in text
    assert f"Сбор за {older:%d.%m.%Y}" in text
    assert text.index(f"Сбор за {newer:%d.%m.%Y}") < text.index(
        f"Сбор за {older:%d.%m.%Y}"
    )
    assert f"Завершён {completed_date:%d.%m.%Y}" in text
    assert "Москва" in text
    assert "Региональный" in text
