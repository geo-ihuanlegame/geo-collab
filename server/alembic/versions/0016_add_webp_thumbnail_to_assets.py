"""为 assets 添加 WebP 和缩略图字段

修订 ID: 0016
上一修订: 0015
创建日期: 2026-05-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("assets", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("webp_storage_key", sa.String(500), nullable=True)
        )
        batch_op.add_column(
            sa.Column("webp_size", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("thumb_storage_key", sa.String(500), nullable=True)
        )
        batch_op.add_column(
            sa.Column("thumb_size", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("assets", schema=None) as batch_op:
        batch_op.drop_column("thumb_size")
        batch_op.drop_column("thumb_storage_key")
        batch_op.drop_column("webp_size")
        batch_op.drop_column("webp_storage_key")
