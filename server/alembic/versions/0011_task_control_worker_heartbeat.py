"""Add task control fields and worker heartbeats

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("publish_tasks", sa.Column("worker_heartbeat_at", sa.DateTime(), nullable=True))
    op.add_column(
        "publish_tasks",
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("browser_sessions", sa.Column("platform_code", sa.String(length=80), nullable=True))

    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=100), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("worker_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index(op.f("ix_worker_heartbeats_heartbeat_at"), "worker_heartbeats", ["heartbeat_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_worker_heartbeats_heartbeat_at"), table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
    op.drop_column("browser_sessions", "platform_code")
    op.drop_column("publish_tasks", "cancel_requested")
    op.drop_column("publish_tasks", "worker_heartbeat_at")
