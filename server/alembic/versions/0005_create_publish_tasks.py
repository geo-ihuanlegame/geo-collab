"""create publish_tasks, publish_task_accounts, publish_records, task_logs

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "publish_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("task_type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="pending"),
        sa.Column("platform_id", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=True),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("stop_before_publish", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("client_request_id", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "task_type in ('single', 'group_round_robin')",
            name="ck_publish_tasks_task_type",
        ),
        sa.CheckConstraint(
            "status in ('pending', 'running', 'succeeded', 'partial_failed', 'failed', 'cancelled')",
            name="ck_publish_tasks_status",
        ),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"]),
        sa.ForeignKeyConstraint(["group_id"], ["article_groups.id"]),
        sa.ForeignKeyConstraint(["platform_id"], ["platforms.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_request_id", name="uq_publish_tasks_client_request_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index(op.f("ix_publish_tasks_platform_id"), "publish_tasks", ["platform_id"], unique=False)
    op.create_index(op.f("ix_publish_tasks_status"), "publish_tasks", ["status"], unique=False)
    op.create_index(op.f("ix_publish_tasks_task_type"), "publish_tasks", ["task_type"], unique=False)

    op.create_table(
        "publish_task_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["publish_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "account_id", name="uq_publish_task_accounts_task_account"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index(op.f("ix_publish_task_accounts_account_id"), "publish_task_accounts", ["account_id"], unique=False)
    op.create_index(op.f("ix_publish_task_accounts_task_id"), "publish_task_accounts", ["task_id"], unique=False)

    op.create_table(
        "publish_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("platform_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="pending"),
        sa.Column("publish_url", sa.String(length=1000), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_of_record_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status in ('pending', 'running', 'waiting_manual_publish', "
            "'waiting_user_input', 'succeeded', 'failed', 'cancelled')",
            name="ck_publish_records_status",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"]),
        sa.ForeignKeyConstraint(["platform_id"], ["platforms.id"]),
        sa.ForeignKeyConstraint(["retry_of_record_id"], ["publish_records.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["publish_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index(op.f("ix_publish_records_account_id"), "publish_records", ["account_id"], unique=False)
    op.create_index(op.f("ix_publish_records_article_id"), "publish_records", ["article_id"], unique=False)
    op.create_index(op.f("ix_publish_records_platform_id"), "publish_records", ["platform_id"], unique=False)
    op.create_index(op.f("ix_publish_records_status"), "publish_records", ["status"], unique=False)
    op.create_index(op.f("ix_publish_records_task_id"), "publish_records", ["task_id"], unique=False)

    op.create_table(
        "task_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("record_id", sa.Integer(), nullable=True),
        sa.Column("level", sa.String(length=20), nullable=False, server_default="info"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("screenshot_asset_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("level in ('info', 'warn', 'error')", name="ck_task_logs_level"),
        sa.ForeignKeyConstraint(["record_id"], ["publish_records.id"]),
        sa.ForeignKeyConstraint(["screenshot_asset_id"], ["assets.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["publish_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index(op.f("ix_task_logs_level"), "task_logs", ["level"], unique=False)
    op.create_index(op.f("ix_task_logs_task_id"), "task_logs", ["task_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_task_logs_task_id"), table_name="task_logs")
    op.drop_index(op.f("ix_task_logs_level"), table_name="task_logs")
    op.drop_table("task_logs")
    op.drop_index(op.f("ix_publish_records_task_id"), table_name="publish_records")
    op.drop_index(op.f("ix_publish_records_status"), table_name="publish_records")
    op.drop_index(op.f("ix_publish_records_platform_id"), table_name="publish_records")
    op.drop_index(op.f("ix_publish_records_article_id"), table_name="publish_records")
    op.drop_index(op.f("ix_publish_records_account_id"), table_name="publish_records")
    op.drop_table("publish_records")
    op.drop_index(op.f("ix_publish_task_accounts_task_id"), table_name="publish_task_accounts")
    op.drop_index(op.f("ix_publish_task_accounts_account_id"), table_name="publish_task_accounts")
    op.drop_table("publish_task_accounts")
    op.drop_index(op.f("ix_publish_tasks_task_type"), table_name="publish_tasks")
    op.drop_index(op.f("ix_publish_tasks_status"), table_name="publish_tasks")
    op.drop_index(op.f("ix_publish_tasks_platform_id"), table_name="publish_tasks")
    op.drop_table("publish_tasks")
