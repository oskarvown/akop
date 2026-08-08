"""stage4.4 comment enrichment jobs, analyses, and ENRICHED artifact linkage

Revision ID: g9b700000001
Revises: f8a600000001
Create Date: 2026-08-08

Self-contained: does not import application modules.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "g9b700000001"
down_revision: str | None = "f8a600000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    job_status = postgresql.ENUM(
        "pending",
        "claimed",
        "ready",
        "failed",
        "skipped",
        name="comment_enrichment_job_status",
        create_type=False,
    )
    analysis_source = postgresql.ENUM(
        "deterministic",
        "llm",
        "unparsed",
        name="comment_analysis_source",
        create_type=False,
    )
    analysis_status = postgresql.ENUM(
        "resolved",
        "needs_review",
        name="comment_analysis_status",
        create_type=False,
    )
    analysis_confidence = postgresql.ENUM(
        "high",
        "medium",
        "low",
        "none",
        name="comment_analysis_confidence",
        create_type=False,
    )
    job_status.create(op.get_bind(), checkfirst=True)
    analysis_source.create(op.get_bind(), checkfirst=True)
    analysis_status.create(op.get_bind(), checkfirst=True)
    analysis_confidence.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "comment_enrichment_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "audit_report_id",
            sa.Integer(),
            sa.ForeignKey("audit_reports.id"),
            nullable=False,
        ),
        sa.Column(
            "comment_analysis_batch_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("enrichment_input_hash", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending",
                "claimed",
                "ready",
                "failed",
                "skipped",
                name="comment_enrichment_job_status",
                create_type=False,
            ),
            nullable=False,
        ),
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
            "operator_retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_terminal_error", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("schema_version_llm", sa.Text(), nullable=False),
        sa.Column("redaction_version", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column(
            "input_snapshot_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "enrichment_counts_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("skipped_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "audit_report_id",
            "enrichment_input_hash",
            name="uq_comment_enrichment_job_report_hash",
        ),
        sa.UniqueConstraint(
            "comment_analysis_batch_id",
            name="uq_comment_enrichment_job_batch_id",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_comment_enrichment_job_attempt_count",
        ),
        sa.CheckConstraint(
            "operator_retry_count >= 0",
            name="ck_comment_enrichment_job_operator_retry_count",
        ),
        sa.CheckConstraint(
            "("
            " (status = 'claimed' AND claim_token IS NOT NULL AND claimed_at IS NOT NULL)"
            " OR "
            " (status <> 'claimed' AND claim_token IS NULL AND claimed_at IS NULL)"
            ")",
            name="ck_comment_enrichment_job_claim_fields",
        ),
    )

    op.create_table(
        "comment_analyses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "enrichment_job_id",
            sa.Integer(),
            sa.ForeignKey("comment_enrichment_jobs.id"),
            nullable=False,
        ),
        sa.Column(
            "debt_position_id",
            sa.Integer(),
            sa.ForeignKey("debt_positions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("analysis_input_hash", sa.Text(), nullable=False),
        sa.Column("comment_raw", sa.Text(), nullable=False),
        sa.Column(
            "source",
            postgresql.ENUM(
                "deterministic",
                "llm",
                "unparsed",
                name="comment_analysis_source",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "analysis_status",
            postgresql.ENUM(
                "resolved",
                "needs_review",
                name="comment_analysis_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "confidence",
            postgresql.ENUM(
                "high",
                "medium",
                "low",
                "none",
                name="comment_analysis_confidence",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("mentioned_date", sa.Date(), nullable=True),
        sa.Column("mentioned_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("responsible_person", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "raw_llm_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("parse_notes", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "enrichment_job_id",
            "debt_position_id",
            name="uq_comment_analysis_job_position",
        ),
    )

    op.drop_index(
        "uq_audit_artifact_comment_analysis_batch_id",
        table_name="audit_artifacts",
    )
    op.drop_column("audit_artifacts", "comment_analysis_batch_id")
    op.add_column(
        "audit_artifacts",
        sa.Column(
            "enrichment_job_id",
            sa.Integer(),
            sa.ForeignKey("comment_enrichment_jobs.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "uq_audit_artifact_enrichment_job_id",
        "audit_artifacts",
        ["enrichment_job_id"],
        unique=True,
        postgresql_where=sa.text("enrichment_job_id IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_audit_artifact_enrichment_job_kind",
        "audit_artifacts",
        "("
        " (kind = 'enriched' AND enrichment_job_id IS NOT NULL)"
        " OR "
        " (kind = 'core' AND enrichment_job_id IS NULL)"
        ")",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_audit_artifact_enrichment_job_kind",
        "audit_artifacts",
        type_="check",
    )
    op.drop_index(
        "uq_audit_artifact_enrichment_job_id",
        table_name="audit_artifacts",
    )
    op.drop_column("audit_artifacts", "enrichment_job_id")
    op.add_column(
        "audit_artifacts",
        sa.Column(
            "comment_analysis_batch_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_index(
        "uq_audit_artifact_comment_analysis_batch_id",
        "audit_artifacts",
        ["comment_analysis_batch_id"],
        unique=True,
        postgresql_where=sa.text("comment_analysis_batch_id IS NOT NULL"),
    )
    op.drop_table("comment_analyses")
    op.drop_table("comment_enrichment_jobs")
    sa.Enum(name="comment_analysis_confidence").drop(
        op.get_bind(), checkfirst=True
    )
    sa.Enum(name="comment_analysis_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="comment_analysis_source").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="comment_enrichment_job_status").drop(
        op.get_bind(), checkfirst=True
    )
