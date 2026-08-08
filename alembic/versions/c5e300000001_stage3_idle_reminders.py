"""stage3 idle reminders and expiration fields

Revision ID: c5e300000001
Revises: b4d200000001
Create Date: 2026-08-08

Does not import app.config / get_settings. Existing notification_chat_id rows
are backfilled via a temporary server_default of 743971617, then the default
is dropped so new cycles are filled only by the application.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c5e300000001"
down_revision: Union[str, None] = "b4d200000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Deterministic backfill for rows created before Stage 3.2 (Sasha's chat id).
_LEGACY_NOTIFICATION_CHAT_ID = 743971617


def upgrade() -> None:
    op.add_column(
        "audit_cycles",
        sa.Column(
            "notification_chat_id",
            sa.BigInteger(),
            nullable=True,
            server_default=sa.text(str(_LEGACY_NOTIFICATION_CHAT_ID)),
        ),
    )
    op.execute(
        "UPDATE audit_cycles "
        f"SET notification_chat_id = {_LEGACY_NOTIFICATION_CHAT_ID} "
        "WHERE notification_chat_id IS NULL"
    )
    op.alter_column(
        "audit_cycles",
        "notification_chat_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.alter_column(
        "audit_cycles",
        "notification_chat_id",
        server_default=None,
    )

    op.add_column(
        "audit_cycles",
        sa.Column(
            "reminder_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "audit_cycles",
        sa.Column("last_reminder_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "audit_cycles",
        sa.Column("reminder_claim_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "audit_cycles",
        sa.Column("reminder_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "audit_cycles",
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audit_cycles", "expired_at")
    op.drop_column("audit_cycles", "reminder_claimed_at")
    op.drop_column("audit_cycles", "reminder_claim_token")
    op.drop_column("audit_cycles", "last_reminder_at")
    op.drop_column("audit_cycles", "reminder_count")
    op.drop_column("audit_cycles", "notification_chat_id")
