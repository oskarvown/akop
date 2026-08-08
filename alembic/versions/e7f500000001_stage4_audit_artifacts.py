"""stage4.2 audit_artifacts for CORE/ENRICHED excel bytes

Revision ID: e7f500000001
Revises: d6e400000001
Create Date: 2026-08-08

Self-contained: does not import application modules.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e7f500000001"
down_revision: str | None = "d6e400000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    artifact_kind = postgresql.ENUM(
        "core",
        "enriched",
        name="audit_artifact_kind",
        create_type=False,
    )
    artifact_kind.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "audit_artifacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "audit_report_id",
            sa.Integer(),
            sa.ForeignKey("audit_reports.id"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            postgresql.ENUM(
                "core",
                "enriched",
                name="audit_artifact_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("excel_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("excel_sha256", sa.Text(), nullable=False),
        sa.Column("financial_input_hash", sa.Text(), nullable=False),
        sa.Column("enrichment_input_hash", sa.Text(), nullable=True),
        sa.Column("generator_version", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column(
            "comment_analysis_batch_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("schema_version_llm", sa.Text(), nullable=True),
        sa.Column("redaction_version", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "audit_report_id",
            "kind",
            "revision",
            name="uq_audit_artifact_report_kind_revision",
        ),
    )
    op.create_index(
        "uq_audit_artifact_comment_analysis_batch_id",
        "audit_artifacts",
        ["comment_analysis_batch_id"],
        unique=True,
        postgresql_where=sa.text("comment_analysis_batch_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_audit_artifact_comment_analysis_batch_id",
        table_name="audit_artifacts",
    )
    op.drop_table("audit_artifacts")
    op.execute("DROP TYPE IF EXISTS audit_artifact_kind")
