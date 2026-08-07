"""stage3 weekly audit cycle

Revision ID: a3c100000001
Revises: 7837bb75981e
Create Date: 2026-08-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a3c100000001"
down_revision: Union[str, None] = "7837bb75981e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


AUDIT_CYCLE_STATUS_VALUES = ("collecting", "completed", "expired")
SOURCE_FILE_LIFECYCLE_VALUES = ("active", "superseded")


def upgrade() -> None:
    bind = op.get_bind()

    audit_cycle_status = postgresql.ENUM(
        *AUDIT_CYCLE_STATUS_VALUES,
        name="audit_cycle_status",
    )
    source_file_lifecycle = postgresql.ENUM(
        *SOURCE_FILE_LIFECYCLE_VALUES,
        name="source_file_lifecycle",
    )
    audit_cycle_status.create(bind, checkfirst=True)
    source_file_lifecycle.create(bind, checkfirst=True)

    op.create_table(
        "audit_cycles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                *AUDIT_CYCLE_STATUS_VALUES,
                name="audit_cycle_status",
                create_type=False,
            ),
            server_default="collecting",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("report_date", name="uq_audit_cycle_report_date"),
    )

    op.add_column(
        "source_files",
        sa.Column("audit_cycle_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_source_files_audit_cycle_id",
        "source_files",
        "audit_cycles",
        ["audit_cycle_id"],
        ["id"],
    )
    op.add_column(
        "source_files",
        sa.Column(
            "lifecycle_status",
            postgresql.ENUM(
                *SOURCE_FILE_LIFECYCLE_VALUES,
                name="source_file_lifecycle",
                create_type=False,
            ),
            server_default="active",
            nullable=True,
        ),
    )
    op.alter_column(
        "source_files",
        "lifecycle_status",
        existing_type=postgresql.ENUM(
            *SOURCE_FILE_LIFECYCLE_VALUES,
            name="source_file_lifecycle",
            create_type=False,
        ),
        nullable=False,
        existing_server_default="active",
    )
    op.create_index(
        "uq_source_file_active_per_department",
        "source_files",
        ["audit_cycle_id", "department"],
        unique=True,
        postgresql_where=sa.text("lifecycle_status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_source_file_active_per_department",
        table_name="source_files",
    )
    op.drop_column("source_files", "lifecycle_status")
    op.drop_constraint(
        "fk_source_files_audit_cycle_id",
        "source_files",
        type_="foreignkey",
    )
    op.drop_column("source_files", "audit_cycle_id")
    op.drop_table("audit_cycles")

    bind = op.get_bind()
    postgresql.ENUM(
        *SOURCE_FILE_LIFECYCLE_VALUES,
        name="source_file_lifecycle",
    ).drop(bind, checkfirst=True)
    postgresql.ENUM(
        *AUDIT_CYCLE_STATUS_VALUES,
        name="audit_cycle_status",
    ).drop(bind, checkfirst=True)
