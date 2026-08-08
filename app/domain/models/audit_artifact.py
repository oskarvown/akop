"""AuditArtifact — immutable Excel bytes for CORE / ENRICHED report revisions."""
from __future__ import annotations

import datetime as dt
import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base

if TYPE_CHECKING:
    pass


class AuditArtifactKind(str, enum.Enum):
    CORE = "core"
    ENRICHED = "enriched"


class AuditArtifact(Base):
    __tablename__ = "audit_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "audit_report_id",
            "kind",
            "revision",
            name="uq_audit_artifact_report_kind_revision",
        ),
        CheckConstraint("revision >= 1", name="ck_audit_artifact_revision_ge_1"),
        CheckConstraint(
            "(kind <> 'core') OR (revision = 1)",
            name="ck_audit_artifact_core_revision_eq_1",
        ),
        Index(
            "uq_audit_artifact_comment_analysis_batch_id",
            "comment_analysis_batch_id",
            unique=True,
            postgresql_where=text("comment_analysis_batch_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    audit_report_id: Mapped[int] = mapped_column(
        ForeignKey("audit_reports.id"), nullable=False
    )
    kind: Mapped[AuditArtifactKind] = mapped_column(
        Enum(
            AuditArtifactKind,
            name="audit_artifact_kind",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
            validate_strings=True,
        ),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    excel_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    excel_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    financial_input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    enrichment_input_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    generator_version: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)

    comment_analysis_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_version_llm: Mapped[str | None] = mapped_column(Text, nullable=True)
    redaction_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
