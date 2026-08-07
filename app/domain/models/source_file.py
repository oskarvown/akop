"""`SourceFile` — см. `docs/DATA_CONTRACT.md` §2.

Stage 2 хранит только то, что нужно парсеру/валидатору одного файла:
принадлежность к отделу, отчётную дату, checksum (дедуп дублей — Roadmap,
уточнено `docs/DATA_CONTRACT.md` §1), статус структурной валидации и
диагностику reconciliation (§6). Комплектность аудита (`AuditCycle`,
5 отделов на дату) — Stage 3, отдельная сущность, здесь не создаётся.
"""
from __future__ import annotations

import datetime as dt
import enum
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Date, DateTime, Enum, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import Department
from app.infrastructure.database.base import Base

if TYPE_CHECKING:
    from app.domain.models.audit_cycle import AuditCycle
    from app.domain.models.debt_position import DebtPosition


class SourceFileStatus(str, enum.Enum):
    """`INVALID` зарезервирован на будущее (Stage 3: история/аудит отклонённых
    загрузок) и в Stage 2 **не используется** — `app.infrastructure.excel.persistence`
    не создаёт `SourceFile` для невалидного файла вообще (см. docstring
    `persist_valid_source_file` и
    `tests/integration/test_excel_persistence_postgres.py::test_rejecting_invalid_file_does_not_persist`).
    """

    VALID = "valid"
    INVALID = "invalid"


class SourceFileLifecycle(str, enum.Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class SourceFile(Base):
    __tablename__ = "source_files"
    __table_args__ = (UniqueConstraint("sha256", name="uq_source_file_sha256"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    audit_cycle_id: Mapped[int | None] = mapped_column(
        ForeignKey("audit_cycles.id", name="fk_source_files_audit_cycle_id"),
        nullable=True,
    )
    department: Mapped[Department] = mapped_column(Enum(Department, name="department"), nullable=False)
    report_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[SourceFileStatus] = mapped_column(
        Enum(SourceFileStatus, name="source_file_status"), nullable=False
    )
    rejection_reasons: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    reported_grand_totals: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reconciliation_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    parsed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lifecycle_status: Mapped[SourceFileLifecycle] = mapped_column(
        Enum(
            SourceFileLifecycle,
            name="source_file_lifecycle",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
            validate_strings=True,
        ),
        nullable=False,
        default=SourceFileLifecycle.ACTIVE,
        server_default=SourceFileLifecycle.ACTIVE.value,
    )

    debt_positions: Mapped[list["DebtPosition"]] = relationship(back_populates="source_file")
    audit_cycle: Mapped["AuditCycle | None"] = relationship(back_populates="source_files")
