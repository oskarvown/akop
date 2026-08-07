"""Weekly accounts-receivable audit cycle grouped by report date."""
from __future__ import annotations

import datetime as dt
import enum
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, Enum, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base

if TYPE_CHECKING:
    from app.domain.models.source_file import SourceFile


class AuditCycleStatus(str, enum.Enum):
    COLLECTING = "collecting"
    COMPLETED = "completed"
    EXPIRED = "expired"


class AuditCycle(Base):
    __tablename__ = "audit_cycles"
    __table_args__ = (
        UniqueConstraint("report_date", name="uq_audit_cycle_report_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    report_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[AuditCycleStatus] = mapped_column(
        Enum(
            AuditCycleStatus,
            name="audit_cycle_status",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
            validate_strings=True,
        ),
        nullable=False,
        default=AuditCycleStatus.COLLECTING,
        server_default=AuditCycleStatus.COLLECTING.value,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_activity_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    source_files: Mapped[list[SourceFile]] = relationship(back_populates="audit_cycle")
