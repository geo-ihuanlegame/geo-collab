"""create article_groups, article_group_items

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "article_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index(op.f("ix_article_groups_name"), "article_groups", ["name"], unique=True)

    op.create_table(
        "article_group_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"]),
        sa.ForeignKeyConstraint(["group_id"], ["article_groups.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "article_id", name="uq_article_group_items_group_article"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index(op.f("ix_article_group_items_group_id"), "article_group_items", ["group_id"], unique=False)
    op.create_index(op.f("ix_article_group_items_article_id"), "article_group_items", ["article_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_article_group_items_article_id"), table_name="article_group_items")
    op.drop_index(op.f("ix_article_group_items_group_id"), table_name="article_group_items")
    op.drop_table("article_group_items")
    op.drop_index(op.f("ix_article_groups_name"), table_name="article_groups")
    op.drop_table("article_groups")
