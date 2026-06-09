"""为 users 添加 display_name 和 feishu_open_id

修订 ID: 0013
上一修订: 0012
创建日期: 2026-05-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("display_name", sa.String(200), nullable=True))
    op.add_column("users", sa.Column("feishu_open_id", sa.String(200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("feishu_open_id")
        batch_op.drop_column("display_name")
