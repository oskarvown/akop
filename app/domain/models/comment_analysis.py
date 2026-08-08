"""CommentAnalysis — immutable per-comment result within an enrichment job."""
from __future__ import annotations

import datetime as dt
import enum
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Date,
    Enum,
    ForeignKey,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base

MONEY = Numeric(18, 2)


class CommentAnalysisSource(str, enum.Enum):
    DETERMINISTIC = "deterministic"
    LLM = "llm"
    UNPARSED = "unparsed"


class CommentAnalysisStatus(str, enum.Enum):
    RESOLVED = "resolved"
    NEEDS_REVIEW = "needs_review"


class CommentAnalysisConfidence(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class CommentAnalysis(Base):
    __tablename__ = "comment_analyses"
    __table_args__ = (
        UniqueConstraint(
            "enrichment_job_id",
            "debt_position_id",
            name="uq_comment_analysis_job_position",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    enrichment_job_id: Mapped[int] = mapped_column(
        ForeignKey("comment_enrichment_jobs.id"), nullable=False
    )
    debt_position_id: Mapped[int] = mapped_column(
        ForeignKey("debt_positions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    analysis_input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    comment_raw: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[CommentAnalysisSource] = mapped_column(
        Enum(
            CommentAnalysisSource,
            name="comment_analysis_source",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
            validate_strings=True,
        ),
        nullable=False,
    )
    analysis_status: Mapped[CommentAnalysisStatus] = mapped_column(
        Enum(
            CommentAnalysisStatus,
            name="comment_analysis_status",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
            validate_strings=True,
        ),
        nullable=False,
    )
    confidence: Mapped[CommentAnalysisConfidence] = mapped_column(
        Enum(
            CommentAnalysisConfidence,
            name="comment_analysis_confidence",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
            validate_strings=True,
        ),
        nullable=False,
    )
    mentioned_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    mentioned_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    action: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_person: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_llm_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    parse_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
