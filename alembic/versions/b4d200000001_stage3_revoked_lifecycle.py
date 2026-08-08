"""stage3 revoked lifecycle and sha256 partial unique

Revision ID: b4d200000001
Revises: a3c100000001
Create Date: 2026-08-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b4d200000001"
down_revision: Union[str, None] = "a3c100000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


OLD_LIFECYCLE_VALUES = ("active", "superseded")


def upgrade() -> None:
    op.execute(
        "ALTER TYPE source_file_lifecycle ADD VALUE IF NOT EXISTS 'revoked'"
    )

    op.drop_constraint("uq_source_file_sha256", "source_files", type_="unique")
    op.create_index(
        "uq_source_file_sha256_active_or_superseded",
        "source_files",
        ["sha256"],
        unique=True,
        postgresql_where=sa.text(
            "lifecycle_status IN ('active', 'superseded')"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.execute(
        "UPDATE source_files SET lifecycle_status = 'superseded' "
        "WHERE lifecycle_status::text = 'revoked'"
    )

    op.drop_index(
        "uq_source_file_sha256_active_or_superseded",
        table_name="source_files",
    )
    # Predicate indexes bind to the enum type; drop before shrinking enum.
    op.drop_index(
        "uq_source_file_active_per_department",
        table_name="source_files",
    )
    op.create_unique_constraint(
        "uq_source_file_sha256",
        "source_files",
        ["sha256"],
    )

    old_enum = postgresql.ENUM(
        *OLD_LIFECYCLE_VALUES,
        name="source_file_lifecycle_old",
    )
    old_enum.create(bind, checkfirst=True)

    op.execute("ALTER TABLE source_files ALTER COLUMN lifecycle_status DROP DEFAULT")
    op.execute(
        "ALTER TABLE source_files "
        "ALTER COLUMN lifecycle_status TYPE source_file_lifecycle_old "
        "USING (lifecycle_status::text::source_file_lifecycle_old)"
    )

    op.execute("DROP TYPE source_file_lifecycle")
    op.execute("ALTER TYPE source_file_lifecycle_old RENAME TO source_file_lifecycle")

    op.execute(
        "ALTER TABLE source_files "
        "ALTER COLUMN lifecycle_status SET DEFAULT 'active'::source_file_lifecycle"
    )
    op.create_index(
        "uq_source_file_active_per_department",
        "source_files",
        ["audit_cycle_id", "department"],
        unique=True,
        postgresql_where=sa.text("lifecycle_status = 'active'"),
    )
