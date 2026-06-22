"""auto_review_decisions：Loop 自动评分记录；articles 加 metrics JSON 列（回流用）。

修订 ID: 0048
上一修订: 0047
创建日期: 2026-06-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)

    if "auto_review_decisions" not in inspector.get_table_names():
        op.create_table(
            "auto_review_decisions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("article_id", sa.Integer(), nullable=False),
            sa.Column("decision", sa.String(20), nullable=False),
            sa.Column("score_total", sa.Integer(), nullable=True),
            sa.Column("score_breakdown", sa.JSON(), nullable=True),
            sa.Column("reasoning", sa.Text(), nullable=True),
            sa.Column("decided_by", sa.String(50), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_auto_review_decisions_article_created",
            "auto_review_decisions",
            ["article_id", sa.text("created_at DESC")],
        )
        op.create_foreign_key(
            "fk_auto_review_decisions_article",
            "auto_review_decisions",
            "articles",
            ["article_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # 给 articles 加 metrics 列（如果还没有）
    article_cols = {c["name"] for c in inspector.get_columns("articles")}
    if "metrics" not in article_cols:
        op.add_column("articles", sa.Column("metrics", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if "auto_review_decisions" in inspector.get_table_names():
        op.drop_constraint(
            "fk_auto_review_decisions_article",
            "auto_review_decisions",
            type_="foreignkey",
        )
        op.drop_index("ix_auto_review_decisions_article_created", table_name="auto_review_decisions")
        op.drop_table("auto_review_decisions")
    article_cols = {c["name"] for c in inspector.get_columns("articles")}
    if "metrics" in article_cols:
        op.drop_column("articles", "metrics")
