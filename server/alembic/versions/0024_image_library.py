"""图片库：stock_categories、stock_images、articles.stock_category_id

修订 ID: 0024
上一修订: 0023
创建日期: 2026-05-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stock_categories",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("bucket_name", sa.String(63), nullable=False, unique=True),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "stock_images",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("stock_categories.id"), nullable=False, index=True),
        sa.Column("minio_key", sa.String(500), nullable=False, unique=True),
        sa.Column("filename", sa.String(300), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    with op.batch_alter_table("articles", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("stock_category_id", sa.Integer(), sa.ForeignKey("stock_categories.id"), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("articles", schema=None) as batch_op:
        batch_op.drop_column("stock_category_id")

    op.drop_table("stock_images")
    op.drop_table("stock_categories")
