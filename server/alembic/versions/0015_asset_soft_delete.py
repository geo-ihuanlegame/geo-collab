"""为 assets 添加 is_deleted / deleted_at 软删除字段

修订 ID: 0015
上一修订: 0014
创建日期: 2026-05-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("assets", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("deleted_at", sa.DateTime(), nullable=True)
        )
        batch_op.create_index("ix_assets_is_deleted", ["is_deleted"])


def downgrade() -> None:
    with op.batch_alter_table("assets", schema=None) as batch_op:
        batch_op.drop_index("ix_assets_is_deleted")
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("is_deleted")
