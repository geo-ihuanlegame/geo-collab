"""为 articles 添加 review_status

修订 ID: 0037
上一修订: 0036
创建日期: 2026-06-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if "articles" not in set(inspector.get_table_names()):
        return
    if "review_status" in _columns(inspector, "articles"):
        return
    with op.batch_alter_table("articles", schema=None) as batch_op:
        # 既有行经 server_default 自动落为 approved（不破坏现有发布）。
        batch_op.add_column(
            sa.Column(
                "review_status",
                sa.String(20),
                nullable=False,
                server_default="approved",
            )
        )
        batch_op.create_index(
            batch_op.f("ix_articles_review_status"), ["review_status"], unique=False
        )
        batch_op.create_check_constraint(
            "ck_articles_review_status",
            "review_status in ('pending', 'approved')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if "articles" not in set(inspector.get_table_names()):
        return
    if "review_status" not in _columns(inspector, "articles"):
        return
    with op.batch_alter_table("articles", schema=None) as batch_op:
        batch_op.drop_constraint("ck_articles_review_status", type_="check")
        batch_op.drop_index(batch_op.f("ix_articles_review_status"))
        batch_op.drop_column("review_status")
