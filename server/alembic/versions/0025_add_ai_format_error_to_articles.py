"""为 articles 添加 ai_format_error

修订 ID: 0025
上一修订: 0024
创建日期: 2026-05-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("articles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("ai_format_error", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("articles", schema=None) as batch_op:
        batch_op.drop_column("ai_format_error")
