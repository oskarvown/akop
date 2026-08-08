"""ReportDelivery — Telegram send lifecycle for CORE/ENRICHED artifacts (Stage 4.3).

Delivery is at-least-once: Telegram has no idempotency key, so a crash between a
successful Bot API response and the DB commit that records it may send one message
twice. Partial progress (summary_sent_count / document_message_id) prevents
re-sending already recorded steps on retry.
"""
from __future__ import annotations

import datetime as dt
import enum
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class ReportDeliveryChannel(str, enum.Enum):
    TELEGRAM = "telegram"


class ReportDeliveryKind(str, enum.Enum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class ReportDeliveryStatus(str, enum.Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    DELIVERED = "delivered"
    FAILED = "failed"


class ReportDelivery(Base):
    __tablename__ = "report_deliveries"
    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="ck_report_delivery_attempt_count"),
        CheckConstraint(
            "summary_sent_count >= 0",
            name="ck_report_delivery_summary_sent_count",
        ),
        CheckConstraint(
            "("
            " (status = 'claimed' AND claim_token IS NOT NULL AND claimed_at IS NOT NULL)"
            " OR "
            " (status <> 'claimed' AND claim_token IS NULL AND claimed_at IS NULL)"
            ")",
            name="ck_report_delivery_claim_fields",
        ),
        CheckConstraint(
            "("
            " status <> 'delivered'"
            " OR (delivered_at IS NOT NULL AND document_message_id IS NOT NULL)"
            ")",
            name="ck_report_delivery_delivered_fields",
        ),
        Index(
            "uq_report_delivery_automatic_artifact_channel",
            "audit_artifact_id",
            "channel",
            unique=True,
            postgresql_where=text("kind = 'automatic'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    audit_artifact_id: Mapped[int] = mapped_column(
        ForeignKey("audit_artifacts.id"), nullable=False
    )
    channel: Mapped[ReportDeliveryChannel] = mapped_column(
        Enum(
            ReportDeliveryChannel,
            name="report_delivery_channel",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
            validate_strings=True,
        ),
        nullable=False,
    )
    kind: Mapped[ReportDeliveryKind] = mapped_column(
        Enum(
            ReportDeliveryKind,
            name="report_delivery_kind",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
            validate_strings=True,
        ),
        nullable=False,
    )
    status: Mapped[ReportDeliveryStatus] = mapped_column(
        Enum(
            ReportDeliveryStatus,
            name="report_delivery_status",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
            validate_strings=True,
        ),
        nullable=False,
    )

    destination_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    requested_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

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

    summary_sent_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    summary_message_ids: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    document_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    delivered_at: Mapped[dt.datetime | None] = mapped_column(
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
