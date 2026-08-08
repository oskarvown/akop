"""AuditReport — orchestrator for Stage 4 deterministic comparison reports.

Excel bytes live on AuditArtifact (Stage 4.2+). This table tracks build claim,
retry, and reproducibility metadata only.
"""
from __future__ import annotations

import datetime as dt
import enum
import uuid
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column


from app.infrastructure.database.base import Base


class AuditReportStatus(str, enum.Enum):
    PENDING = "pending"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"


class AuditReport(Base):
    __tablename__ = "audit_reports"
    __table_args__ = (
        UniqueConstraint("audit_cycle_id", name="uq_audit_report_cycle"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    audit_cycle_id: Mapped[int] = mapped_column(
        ForeignKey("audit_cycles.id"), nullable=False
    )
    previous_cycle_id: Mapped[int | None] = mapped_column(
        ForeignKey("audit_cycles.id"), nullable=True
    )
    status: Mapped[AuditReportStatus] = mapped_column(
        Enum(
            AuditReportStatus,
            name="audit_report_status",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
            validate_strings=True,
        ),
        nullable=False,
        default=AuditReportStatus.PENDING,
        server_default=AuditReportStatus.PENDING.value,
    )

    build_claim_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    build_claimed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    build_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_retry_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_build_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    generator_version: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    built_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
