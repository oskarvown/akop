"""stage4.1 match_key columns and audit_reports

Revision ID: d6e400000001
Revises: c5e300000001
Create Date: 2026-08-08

Safe order for debt_positions match fields:
nullable add → Python backfill → validate → NOT NULL → indexes.
Also creates audit_reports orchestrator table (no Excel BYTEA).

This revision is self-contained: backfill logic is inlined and does not import
application/domain modules (those may change after this migration ships).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d6e400000001"
down_revision: str | None = "c5e300000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_label(raw_name: str) -> str:
    """Frozen Stage 4.1 normalization (NFKC, trim, collapse WS, casefold)."""
    text = unicodedata.normalize("NFKC", raw_name)
    text = text.strip()
    text = _WHITESPACE_RE.sub(" ", text)
    return text.casefold()


def _build_match_key(
    *,
    counterparty_id: int,
    outline_level: int,
    normalized_label: str,
    parent_match_key: str | None,
) -> str:
    if outline_level == 1:
        return f"c:{counterparty_id}"
    if parent_match_key is None:
        raise RuntimeError(
            f"missing parent match_key for outline_level={outline_level} "
            f"counterparty_id={counterparty_id}"
        )
    return f"{parent_match_key}|{outline_level}:{normalized_label}"


def _match_key_hash(match_key: str) -> str:
    return hashlib.sha256(match_key.encode("utf-8")).hexdigest()


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
        sa.Column(
            "summary_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
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
    from sqlalchemy import text

    conn = op.get_bind()
    rows = conn.execute(
        text(
            "SELECT id, counterparty_id, parent_position_id, outline_level, raw_label "
            "FROM debt_positions ORDER BY outline_level ASC, id ASC"
        )
    ).fetchall()

    key_by_id: dict[int, str] = {}
    for row in rows:
        position_id = int(row[0])
        counterparty_id = int(row[1])
        parent_id = row[2]
        outline_level = int(row[3])
        raw_label = str(row[4])
        normalized = _normalize_label(raw_label)
        if outline_level == 1:
            parent_key = None
        else:
            if parent_id is None or int(parent_id) not in key_by_id:
                raise RuntimeError(
                    f"debt_positions.id={position_id} missing parent match_key "
                    f"for outline_level={outline_level} "
                    f"(parent_position_id={parent_id!r})"
                )
            parent_key = key_by_id[int(parent_id)]
        key = _build_match_key(
            counterparty_id=counterparty_id,
            outline_level=outline_level,
            normalized_label=normalized,
            parent_match_key=parent_key,
        )
        key_by_id[position_id] = key
        conn.execute(
            text(
                "UPDATE debt_positions "
                "SET normalized_label = :normalized_label, "
                "match_key = :match_key, "
                "match_key_hash = :match_key_hash "
                "WHERE id = :id"
            ),
            {
                "id": position_id,
                "normalized_label": normalized,
                "match_key": key,
                "match_key_hash": _match_key_hash(key),
            },
        )

    nulls = conn.execute(
        text(
            "SELECT COUNT(*) FROM debt_positions "
            "WHERE normalized_label IS NULL OR match_key IS NULL "
            "OR match_key_hash IS NULL"
        )
    ).scalar()
    if nulls:
        raise RuntimeError(f"match_key backfill left {nulls} NULL rows")


def downgrade() -> None:
    op.drop_table("audit_reports")
    op.execute("DROP TYPE IF EXISTS audit_report_status")

    op.drop_index("ix_debt_positions_match_key", table_name="debt_positions")
    op.drop_index("ix_debt_positions_match_key_hash", table_name="debt_positions")
    op.drop_column("debt_positions", "match_key_hash")
    op.drop_column("debt_positions", "match_key")
    op.drop_column("debt_positions", "normalized_label")
