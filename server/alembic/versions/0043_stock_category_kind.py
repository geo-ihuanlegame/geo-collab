"""图片库栏目加 kind（主推/陪衬）

修订 ID: 0043
上一修订: 0042
创建日期: 2026-06-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stock_categories",
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="companion"),
    )
    op.create_check_constraint(
        "ck_stock_categories_kind",
        "stock_categories",
        "kind in ('main', 'companion')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_stock_categories_kind", "stock_categories", type_="check")
    op.drop_column("stock_categories", "kind")
