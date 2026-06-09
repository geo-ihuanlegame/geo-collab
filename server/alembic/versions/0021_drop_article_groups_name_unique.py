"""删除 article_groups.name 全局唯一索引，保留按用户约束

修订 ID: 0021
上一修订: 0020
创建日期: 2026-05-20
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    try:
        op.drop_index("ix_article_groups_name", table_name="article_groups")
    except Exception:
        pass
    op.create_index("ix_article_groups_name", "article_groups", ["name"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_article_groups_name", table_name="article_groups")
    op.create_index("ix_article_groups_name", "article_groups", ["name"], unique=True)
