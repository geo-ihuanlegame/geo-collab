"""问题库：category / question_text 列与 category_usages 跟踪表

修订 ID: 0032
上一修订: 0031
创建日期: 2026-05-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect


revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)

    # question_items 加 category + question_text（专用字段，避免每次拼 fields）
    if "question_items" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("question_items")}
        if "category" not in cols:
            op.add_column(
                "question_items",
                sa.Column("category", sa.String(length=200), nullable=True),
            )
            op.create_index(op.f("ix_question_items_category"), "question_items", ["category"], unique=False)
        if "question_text" not in cols:
            # TEXT 不能有字面 DEFAULT；设为可空，ORM 写入时填值
            op.add_column(
                "question_items",
                sa.Column("question_text", sa.Text(), nullable=True),
            )

    # category_usages：自动选题"最近没上的板块优先"用
    if "category_usages" not in inspector.get_table_names():
        op.create_table(
            "category_usages",
            sa.Column("pool_id", sa.Integer(), nullable=False),
            sa.Column("category", sa.String(length=200), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["pool_id"], ["question_pools.id"]),
            sa.PrimaryKeyConstraint("pool_id", "category"),
        )

    # generation_sessions 加 pool_id（手动/自动都关联到某个池） + auto_count（自动模式要生几篇）
    if "generation_sessions" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("generation_sessions")}
        if "pool_id" not in cols:
            op.add_column(
                "generation_sessions",
                sa.Column("pool_id", sa.Integer(), nullable=True),
            )
            op.create_foreign_key(
                "fk_gen_sessions_pool", "generation_sessions",
                "question_pools", ["pool_id"], ["id"],
            )
        if "auto_count" not in cols:
            op.add_column(
                "generation_sessions",
                sa.Column("auto_count", sa.Integer(), nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = inspector.get_table_names()

    if "generation_sessions" in tables:
        cols = {c["name"] for c in inspector.get_columns("generation_sessions")}
        if "auto_count" in cols:
            op.drop_column("generation_sessions", "auto_count")
        if "pool_id" in cols:
            try:
                op.drop_constraint("fk_gen_sessions_pool", "generation_sessions", type_="foreignkey")
            except Exception:
                pass
            op.drop_column("generation_sessions", "pool_id")

    if "category_usages" in tables:
        op.drop_table("category_usages")

    if "question_items" in tables:
        cols = {c["name"] for c in inspector.get_columns("question_items")}
        if "question_text" in cols:
            op.drop_column("question_items", "question_text")
        if "category" in cols:
            op.drop_index(op.f("ix_question_items_category"), table_name="question_items")
            op.drop_column("question_items", "category")
