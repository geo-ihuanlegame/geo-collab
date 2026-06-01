"""Add worker-owned account login sessions

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "account_login_sessions",
        sa.Column("id", sa.String(length=12), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("platform_code", sa.String(length=80), nullable=False),
        sa.Column("account_key", sa.String(length=200), nullable=False),
        sa.Column("channel", sa.String(length=80), nullable=False),
        sa.Column("executable_path", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("browser_session_id", sa.String(length=12), nullable=True),
        sa.Column("novnc_url", sa.String(length=500), nullable=True),
        sa.Column("logged_in", sa.Boolean(), nullable=True),
        sa.Column("result_url", sa.String(length=1000), nullable=True),
        sa.Column("result_title", sa.String(length=500), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index(op.f("ix_account_login_sessions_account_id"), "account_login_sessions", ["account_id"], unique=False)
    op.create_index(op.f("ix_account_login_sessions_browser_session_id"), "account_login_sessions", ["browser_session_id"], unique=False)
    op.create_index(op.f("ix_account_login_sessions_status"), "account_login_sessions", ["status"], unique=False)
    op.create_index(op.f("ix_account_login_sessions_worker_id"), "account_login_sessions", ["worker_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_account_login_sessions_worker_id"), table_name="account_login_sessions")
    op.drop_index(op.f("ix_account_login_sessions_status"), table_name="account_login_sessions")
    op.drop_index(op.f("ix_account_login_sessions_browser_session_id"), table_name="account_login_sessions")
    op.drop_index(op.f("ix_account_login_sessions_account_id"), table_name="account_login_sessions")
    op.drop_table("account_login_sessions")
