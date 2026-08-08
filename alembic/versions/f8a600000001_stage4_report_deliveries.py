"""stage4.3 report_deliveries for Telegram CORE/manual delivery

Revision ID: f8a600000001
Revises: e7f500000001
Create Date: 2026-08-08

Self-contained: does not import application modules.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f8a600000001"
down_revision: str | None = "e7f500000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    channel = postgresql.ENUM(
        "telegram",
        name="report_delivery_channel",
        create_type=False,
    )
    kind = postgresql.ENUM(
        "automatic",
        "manual",
        name="report_delivery_kind",
        create_type=False,
    )
    status = postgresql.ENUM(
        "pending",
        "claimed",
        "delivered",
        "failed",
        name="report_delivery_status",
        create_type=False,
    )
    channel.create(op.get_bind(), checkfirst=True)
    kind.create(op.get_bind(), checkfirst=True)
    status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "report_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "audit_artifact_id",
            sa.Integer(),
            sa.ForeignKey("audit_artifacts.id"),
            nullable=False,
        ),
        sa.Column(
            "channel",
            postgresql.ENUM(
                "telegram",
                name="report_delivery_channel",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "kind",
            postgresql.ENUM(
                "automatic",
                "manual",
                name="report_delivery_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending",
                "claimed",
                "delivered",
                "failed",
                name="report_delivery_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("destination_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "summary_sent_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "summary_message_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("document_message_id", sa.BigInteger(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_report_delivery_attempt_count",
        ),
        sa.CheckConstraint(
            "summary_sent_count >= 0",
            name="ck_report_delivery_summary_sent_count",
        ),
        sa.CheckConstraint(
            "("
            " (status = 'claimed' AND claim_token IS NOT NULL AND claimed_at IS NOT NULL)"
            " OR "
            " (status <> 'claimed' AND claim_token IS NULL AND claimed_at IS NULL)"
            ")",
            name="ck_report_delivery_claim_fields",
        ),
        sa.CheckConstraint(
            "("
            " status <> 'delivered'"
            " OR (delivered_at IS NOT NULL AND document_message_id IS NOT NULL)"
            ")",
            name="ck_report_delivery_delivered_fields",
        ),
    )
    op.create_index(
        "uq_report_delivery_automatic_artifact_channel",
        "report_deliveries",
        ["audit_artifact_id", "channel"],
        unique=True,
        postgresql_where=sa.text("kind = 'automatic'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_report_delivery_automatic_artifact_channel",
        table_name="report_deliveries",
    )
    op.drop_table("report_deliveries")
    op.execute("DROP TYPE IF EXISTS report_delivery_status")
    op.execute("DROP TYPE IF EXISTS report_delivery_kind")
    op.execute("DROP TYPE IF EXISTS report_delivery_channel")
