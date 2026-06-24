"""文章生文溯源字段：智能体（pipeline）名 + 模板名（去规范化、仅展示）

修订 ID: 0052
上一修订: 0051
创建日期: 2026-06-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("articles", sa.Column("source_agent_name", sa.String(200), nullable=True))
    op.add_column("articles", sa.Column("source_template_name", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("articles", "source_template_name")
    op.drop_column("articles", "source_agent_name")
