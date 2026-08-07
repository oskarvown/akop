"""Интеграционный тест: сохранение результата парсинга в реальный PostgreSQL.

Требует локальный PostgreSQL, настроенный по `README.md` («Локальная
разработка» → PostgreSQL), с применённой миграцией Stage 2 (`alembic upgrade
head`). Если БД недоступна — тест пропускается (skip), а не падает, чтобы не
блокировать остальной набор тестов в средах без локальной PostgreSQL.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.domain.enums import Department
from app.domain.models import Counterparty, DebtPosition, ManagerGroup, SourceFile, SourceFileStatus
from app.infrastructure.excel.checksum import compute_sha256
from app.infrastructure.excel.persistence import persist_valid_source_file
from app.infrastructure.excel.validator import validate_confirmed_template_file

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "regional"


@pytest.fixture
async def db_session():
    # Не используем закэшированный `get_engine()`/`get_session_maker()`: каждый
    # тест получает свой event loop (`asyncio_default_fixture_loop_scope =
    # function`), а пул asyncpg-соединений привязан к loop, в котором создан —
    # переиспользование глобального кэша между тестами роняет соединение
    # ("attached to a different loop"). Поэтому здесь — отдельный engine на тест,
    # с явным `dispose()` в конце.
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    session_maker = async_sessionmaker(bind=engine, expire_on_commit=False)

    try:
        async with session_maker() as session:
            await session.execute(select(1))
    except (OperationalError, OSError) as exc:  # pragma: no cover - зависит от окружения
        # asyncpg may raise ConnectionRefusedError (OSError) before SQLAlchemy wraps it.
        await engine.dispose()
        pytest.skip(f"Локальный PostgreSQL недоступен: {exc}")

    async with session_maker() as session:
        yield session
        await session.rollback()

    async with session_maker() as cleanup_session:
        await cleanup_session.execute(delete(DebtPosition))
        await cleanup_session.execute(delete(Counterparty))
        await cleanup_session.execute(delete(SourceFile))
        await cleanup_session.execute(delete(ManagerGroup))
        await cleanup_session.commit()

    await engine.dispose()


@pytest.mark.asyncio
async def test_persist_valid_source_file_creates_full_hierarchy(db_session) -> None:
    path = FIXTURES_DIR / "regional_valid_basic.xls"
    result = validate_confirmed_template_file(path)
    assert result.is_valid, result.rejection_reasons

    source_file = await persist_valid_source_file(
        db_session,
        result=result,
        department=Department.REGIONAL,
        sha256=compute_sha256(path),
        original_filename=path.name,
    )
    await db_session.commit()

    assert source_file.id is not None
    assert source_file.status == SourceFileStatus.VALID

    manager_groups = (await db_session.scalars(select(ManagerGroup))).all()
    assert len(manager_groups) == 2

    counterparties = (await db_session.scalars(select(Counterparty))).all()
    assert len(counterparties) == 3

    positions = (await db_session.scalars(select(DebtPosition))).all()
    assert len(positions) == len(result.parsed.debt_rows)

    levels_present = {p.outline_level for p in positions}
    assert levels_present == {1, 2, 3, 4}

    level1_positions = [p for p in positions if p.outline_level == 1]
    for position in level1_positions:
        assert position.parent_position_id is None

    nested_positions = [p for p in positions if p.outline_level in (2, 3, 4)]
    for position in nested_positions:
        assert position.parent_position_id is not None


@pytest.mark.asyncio
async def test_manager_group_identity_stable_across_two_files(db_session) -> None:
    """Один и тот же `(department, normalized_name)` переиспользует `ManagerGroup.id`
    между двумя разными `SourceFile` — см. `docs/DATA_CONTRACT.md` §2.3."""
    path_a = FIXTURES_DIR / "regional_valid_basic.xls"
    path_b = FIXTURES_DIR / "regional_valid_credit_limit_mismatch.xls"

    result_a = validate_confirmed_template_file(path_a)
    result_b = validate_confirmed_template_file(path_b)
    assert result_a.is_valid and result_b.is_valid

    file_a = await persist_valid_source_file(
        db_session,
        result=result_a,
        department=Department.REGIONAL,
        sha256=compute_sha256(path_a),
        original_filename=path_a.name,
    )
    await db_session.commit()

    file_b = await persist_valid_source_file(
        db_session,
        result=result_b,
        department=Department.REGIONAL,
        sha256=compute_sha256(path_b),
        original_filename=path_b.name,
    )
    await db_session.commit()

    assert file_a.id != file_b.id

    manager_groups = (await db_session.scalars(select(ManagerGroup))).all()
    # Оба fixture используют одни и те же названия ManagerGroup — get-or-create
    # должен переиспользовать существующие записи, а не дублировать их.
    assert len(manager_groups) == 2

    counterparties = (await db_session.scalars(select(Counterparty))).all()
    assert len(counterparties) == 3

    positions = (await db_session.scalars(select(DebtPosition))).all()
    assert len({p.source_file_id for p in positions}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("department", list(Department))
async def test_confirmed_template_applies_to_every_department(db_session, department: Department) -> None:
    """Единый подтверждённый шаблон (`docs/DATA_CONTRACT.md` §3, §10 — бизнес-решение
    Александра от 28.07.2026, дополнено отделом «Фокин» §2.4) применяется к файлу
    **любого** из 5 отделов без каких-либо изменений в парсере/валидаторе/persistence.

    Проверяет отсутствие функциональной привязки к `Department.REGIONAL`:
    `validate_confirmed_template_file` вообще не принимает и не использует
    `Department` (одна и та же реализация для всех 5 отделов, включая новый
    `FOKIN` — тест автоматически покрывает его, т.к. параметризован по
    `list(Department)`, а не по жёстко перечисленным значениям), а
    `persist_valid_source_file` требует `department` явным обязательным
    именованным параметром без значения по умолчанию — вызывающая сторона
    (здесь — параметризация теста) обязана передать конкретный отдел, функция
    никогда сама не подставляет `Department.REGIONAL`.
    """
    path = FIXTURES_DIR / "regional_valid_basic.xls"
    result = validate_confirmed_template_file(path)
    assert result.is_valid, result.rejection_reasons

    source_file = await persist_valid_source_file(
        db_session,
        result=result,
        department=department,
        sha256=compute_sha256(path),
        original_filename=path.name,
    )
    await db_session.commit()

    assert source_file.department == department
    assert source_file.status == SourceFileStatus.VALID

    manager_groups = (
        await db_session.scalars(
            select(ManagerGroup).where(ManagerGroup.department == department)
        )
    ).all()
    assert len(manager_groups) == 2

    positions = (
        await db_session.scalars(
            select(DebtPosition).where(DebtPosition.source_file_id == source_file.id)
        )
    ).all()
    assert len(positions) == len(result.parsed.debt_rows)


@pytest.mark.asyncio
async def test_identically_named_manager_group_and_counterparty_not_mixed_across_departments(
    db_session,
) -> None:
    """Одноимённые `ManagerGroup`/`Counterparty` в разных отделах — разные сущности
    (не смешиваются), несмотря на то, что get-or-create ищет их по
    `normalized_name`. Изоляция обеспечивается тем, что уникальность
    `ManagerGroup` — `(department, normalized_name)`
    (`uq_manager_group_identity`), а `Counterparty` привязан к
    `manager_group_id`, который уже несёт конкретный `department`.

    Один и тот же fixture (одинаковые `raw_name` во всех строках) сохраняется
    под каждым из 4 `Department` в одной сессии — если бы изоляция была
    нарушена, get-or-create переиспользовал бы записи первого отдела для
    всех последующих, и итоговое количество строк не выросло бы в 4 раза.
    """
    path = FIXTURES_DIR / "regional_valid_basic.xls"
    result = validate_confirmed_template_file(path)
    assert result.is_valid, result.rejection_reasons

    for department in Department:
        await persist_valid_source_file(
            db_session,
            result=result,
            department=department,
            sha256=compute_sha256(path) + f"::{department.value}",
            original_filename=path.name,
        )
        await db_session.commit()

    manager_groups = (await db_session.scalars(select(ManagerGroup))).all()
    assert len(manager_groups) == 2 * len(Department)
    assert {mg.department for mg in manager_groups} == set(Department)

    counterparties = (await db_session.scalars(select(Counterparty))).all()
    assert len(counterparties) == 3 * len(Department)

    for department in Department:
        department_groups = [mg for mg in manager_groups if mg.department == department]
        assert len(department_groups) == 2
        group_ids = {mg.id for mg in department_groups}
        department_counterparties = [
            c for c in counterparties if c.manager_group_id in group_ids
        ]
        assert len(department_counterparties) == 3


@pytest.mark.asyncio
async def test_rejecting_invalid_file_does_not_persist(db_session) -> None:
    """Семантика `SourceFileStatus.INVALID` в Stage 2 (зафиксировано в отчёте):

    невалидные файлы **вообще не сохраняются** — ни `SourceFile`, ни
    `DebtPosition`. `persist_valid_source_file` — единственная функция
    сохранения в Stage 2, и она осознанно поднимает `ValueError` для
    невалидного `ValidationResult`, а не создаёт запись со `status=invalid`.
    Значение `SourceFileStatus.INVALID` зарезервировано в схеме на будущее
    (Stage 3: аудит/история отклонённых загрузок), но в Stage 2 никогда не
    записывается — ни одна функция этого модуля его не присваивает.
    """
    path = FIXTURES_DIR / "regional_invalid_missing_columns.xlsx"
    result = validate_confirmed_template_file(path)
    assert result.is_valid is False

    with pytest.raises(ValueError):
        await persist_valid_source_file(
            db_session,
            result=result,
            department=Department.REGIONAL,
            sha256=compute_sha256(path),
            original_filename=path.name,
        )

    positions = (await db_session.scalars(select(DebtPosition))).all()
    assert positions == []

    source_files = (await db_session.scalars(select(SourceFile))).all()
    assert source_files == [], (
        "Stage 2 не создаёт SourceFile для невалидного файла — статус 'invalid' "
        "зарезервирован в схеме, но не используется до Stage 3"
    )


@pytest.mark.asyncio
async def test_concurrent_persist_reuses_manager_group_and_counterparty(
    db_session,
) -> None:
    """Parallel saves for one department must not fail on identity unique indexes."""
    path = FIXTURES_DIR / "regional_valid_basic.xls"
    base = validate_confirmed_template_file(path)
    assert base.is_valid and base.parsed is not None

    maker = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)

    async def persist_variant(day: int, sha_suffix: str) -> None:
        result = replace(
            base,
            parsed=replace(base.parsed, report_date=base.parsed.report_date.replace(day=day)),
        )
        async with maker() as session:
            async with session.begin():
                await persist_valid_source_file(
                    session,
                    result=result,
                    department=Department.REGIONAL,
                    sha256=f"{compute_sha256(path)}::{sha_suffix}",
                    original_filename=f"{sha_suffix}.xls",
                )

    await asyncio.gather(
        persist_variant(11, "a"),
        persist_variant(15, "b"),
    )

    manager_groups = (
        await db_session.scalars(
            select(ManagerGroup).where(ManagerGroup.department == Department.REGIONAL)
        )
    ).all()
    assert len(manager_groups) == 2

    counterparties = (await db_session.scalars(select(Counterparty))).all()
    assert len(counterparties) == 3

    files = (await db_session.scalars(select(SourceFile))).all()
    assert len(files) == 2
