"""add article_stock_categories many-to-many table

Revision ID: 0028
Revises: 0027
Create Date: 2026-05-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "article_stock_categories",
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "stock_category_id",
            sa.Integer(),
            sa.ForeignKey("stock_categories.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.UniqueConstraint("article_id", "stock_category_id", name="uq_article_stock_cat"),
    )
    op.create_index(
        "ix_article_stock_categories_article_id",
        "article_stock_categories",
        ["article_id"],
    )
    op.create_index(
        "ix_article_stock_categories_stock_category_id",
        "article_stock_categories",
        ["stock_category_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_article_stock_categories_stock_category_id",
        table_name="article_stock_categories",
    )
    op.drop_index(
        "ix_article_stock_categories_article_id",
        table_name="article_stock_categories",
    )
    op.drop_table("article_stock_categories")
