"""stage4.1 match_key columns and audit_reports

Revision ID: d6e400000001
Revises: c5e300000001
Create Date: 2026-08-08

Safe order for debt_positions match fields:
nullable add → Python backfill → validate → NOT NULL → indexes.
Also creates audit_reports orchestrator table (no Excel BYTEA).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d6e400000001"
down_revision: str | None = "c5e300000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "debt_positions",
        sa.Column("normalized_label", sa.Text(), nullable=True),
    )
    op.add_column(
        "debt_positions",
        sa.Column("match_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "debt_positions",
        sa.Column("match_key_hash", sa.Text(), nullable=True),
    )

    _backfill_match_keys()

    op.alter_column("debt_positions", "normalized_label", nullable=False)
    op.alter_column("debt_positions", "match_key", nullable=False)
    op.alter_column("debt_positions", "match_key_hash", nullable=False)

    op.create_index(
        "ix_debt_positions_match_key_hash",
        "debt_positions",
        ["match_key_hash"],
    )
    op.create_index(
        "ix_debt_positions_match_key",
        "debt_positions",
        ["match_key"],
    )

    audit_report_status = postgresql.ENUM(
        "pending",
        "building",
        "ready",
        "failed",
        name="audit_report_status",
        create_type=False,
    )
    audit_report_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "audit_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "audit_cycle_id",
            sa.Integer(),
            sa.ForeignKey("audit_cycles.id"),
            nullable=False,
        ),
        sa.Column(
            "previous_cycle_id",
            sa.Integer(),
            sa.ForeignKey("audit_cycles.id"),
            nullable=True,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending",
                "building",
                "ready",
                "failed",
                name="audit_report_status",
                create_type=False,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("build_claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("build_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "build_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_build_error", sa.Text(), nullable=True),
        sa.Column("generator_version", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("summary_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("audit_cycle_id", name="uq_audit_report_cycle"),
    )


def _backfill_match_keys() -> None:
    """Backfill in outline_level order so parent match_key is available."""
    from app.domain.matching.match_key import backfill_match_keys_on_connection

    backfill_match_keys_on_connection(op.get_bind())


def downgrade() -> None:
    op.drop_table("audit_reports")
    op.execute("DROP TYPE IF EXISTS audit_report_status")

    op.drop_index("ix_debt_positions_match_key", table_name="debt_positions")
    op.drop_index("ix_debt_positions_match_key_hash", table_name="debt_positions")
    op.drop_column("debt_positions", "match_key_hash")
    op.drop_column("debt_positions", "match_key")
    op.drop_column("debt_positions", "normalized_label")
