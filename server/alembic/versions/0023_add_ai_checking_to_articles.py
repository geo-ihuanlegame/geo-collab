"""为 articles 添加 ai_checking 锁字段

修订 ID: 0023
上一修订: 0022
创建日期: 2026-05-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("articles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("ai_checking", sa.Boolean(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("ai_checking_started_at", sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f("ix_articles_ai_checking"), ["ai_checking"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("articles", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_articles_ai_checking"))
        batch_op.drop_column("ai_checking_started_at")
        batch_op.drop_column("ai_checking")
