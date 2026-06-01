"""Add worker_id to publish_tasks; add browser_sessions and record_browser_sessions tables

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("publish_tasks", sa.Column("worker_id", sa.String(length=100), nullable=True))
    op.add_column("publish_tasks", sa.Column("worker_lease_until", sa.DateTime(), nullable=True))
    op.create_index(op.f("ix_publish_tasks_worker_id"), "publish_tasks", ["worker_id"], unique=False)

    op.create_table(
        "browser_sessions",
        sa.Column("id", sa.String(length=12), nullable=False),
        sa.Column("account_key", sa.String(length=200), nullable=False),
        sa.Column("display", sa.String(length=20), nullable=True),
        sa.Column("novnc_url", sa.String(length=500), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(), nullable=False),
        sa.Column("worker_id", sa.String(length=100), nullable=True),
        sa.Column("keep_alive", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("stop_requested", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )

    op.create_table(
        "record_browser_sessions",
        sa.Column("record_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=12), nullable=False),
        sa.ForeignKeyConstraint(["record_id"], ["publish_records.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["browser_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("record_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )


def downgrade() -> None:
    op.drop_table("record_browser_sessions")
    op.drop_table("browser_sessions")
    op.drop_index(op.f("ix_publish_tasks_worker_id"), table_name="publish_tasks")
    op.drop_column("publish_tasks", "worker_lease_until")
    op.drop_column("publish_tasks", "worker_id")
