"""问题库：question_pools 和 question_items

修订 ID: 0031
上一修订: 0030
创建日期: 2026-05-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect


revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = inspector.get_table_names()

    if "question_pools" not in tables:
        op.create_table(
            "question_pools",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("feishu_app_token", sa.String(length=255), nullable=True),
            sa.Column("feishu_table_id", sa.String(length=255), nullable=True),
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("last_synced_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_question_pools_user_id"), "question_pools", ["user_id"], unique=False)
        op.create_index(op.f("ix_question_pools_is_deleted"), "question_pools", ["is_deleted"], unique=False)

    if "question_items" not in tables:
        op.create_table(
            "question_items",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("pool_id", sa.Integer(), nullable=False),
            sa.Column("record_id", sa.String(length=255), nullable=False),
            sa.Column("fields", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("article_id", sa.Integer(), nullable=True),
            sa.Column("synced_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["pool_id"], ["question_pools.id"]),
            sa.ForeignKeyConstraint(["article_id"], ["articles.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("pool_id", "record_id", name="uq_question_items_pool_record"),
            sa.CheckConstraint("status in ('pending','consumed')", name="ck_question_items_status"),
        )
        op.create_index(op.f("ix_question_items_pool_id"), "question_items", ["pool_id"], unique=False)
        op.create_index(op.f("ix_question_items_status"), "question_items", ["status"], unique=False)

    # 问题库模式：会话记录选中的问题单元
    if "generation_sessions" in tables:
        gen_cols = [c["name"] for c in inspector.get_columns("generation_sessions")]
        if "question_item_ids" not in gen_cols:
            # MySQL 不允许 TEXT 列有 DEFAULT；设为可空，由 ORM 端 default="[]" 处理（同 article_ids）
            op.add_column(
                "generation_sessions",
                sa.Column("question_item_ids", sa.Text(), nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = inspector.get_table_names()

    if "generation_sessions" in tables:
        gen_cols = [c["name"] for c in inspector.get_columns("generation_sessions")]
        if "question_item_ids" in gen_cols:
            op.drop_column("generation_sessions", "question_item_ids")
    if "question_items" in tables:
        op.drop_table("question_items")
    if "question_pools" in tables:
        op.drop_table("question_pools")
