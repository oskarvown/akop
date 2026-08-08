"""CommentEnrichmentJob — immutable enrichment batch per AuditReport (Stage 4.4)."""
from __future__ import annotations

import datetime as dt
import enum
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
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


class CommentEnrichmentJobStatus(str, enum.Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    READY = "ready"
    FAILED = "failed"
    SKIPPED = "skipped"


class CommentEnrichmentJob(Base):
    __tablename__ = "comment_enrichment_jobs"
    __table_args__ = (
        UniqueConstraint(
            "audit_report_id",
            "enrichment_input_hash",
            name="uq_comment_enrichment_job_report_hash",
        ),
        UniqueConstraint(
            "comment_analysis_batch_id",
            name="uq_comment_enrichment_job_batch_id",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_comment_enrichment_job_attempt_count",
        ),
        CheckConstraint(
            "operator_retry_count >= 0",
            name="ck_comment_enrichment_job_operator_retry_count",
        ),
        CheckConstraint(
            "("
            " (status = 'claimed' AND claim_token IS NOT NULL AND claimed_at IS NOT NULL)"
            " OR "
            " (status <> 'claimed' AND claim_token IS NULL AND claimed_at IS NULL)"
            ")",
            name="ck_comment_enrichment_job_claim_fields",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    audit_report_id: Mapped[int] = mapped_column(
        ForeignKey("audit_reports.id"), nullable=False
    )
    comment_analysis_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    enrichment_input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[CommentEnrichmentJobStatus] = mapped_column(
        Enum(
            CommentEnrichmentJobStatus,
            name="comment_enrichment_job_status",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
            validate_strings=True,
        ),
        nullable=False,
    )
    claim_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    claimed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_retry_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator_retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_terminal_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version_llm: Mapped[str] = mapped_column(Text, nullable=False)
    redaction_version: Mapped[str] = mapped_column(Text, nullable=False)
    parser_version: Mapped[str] = mapped_column(Text, nullable=False)

    input_snapshot_json: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False
    )
    enrichment_counts_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )

    ready_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    skipped_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
