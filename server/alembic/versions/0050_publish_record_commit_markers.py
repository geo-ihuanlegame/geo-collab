"""publish_records 加 commit_attempted_at / failure_kind 两列（断网/弱网发布重试）。

修订 ID: 0050
上一修订: 0049
创建日期: 2026-06-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("publish_records", sa.Column("commit_attempted_at", sa.DateTime(), nullable=True))
    op.add_column("publish_records", sa.Column("failure_kind", sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column("publish_records", "failure_kind")
    op.drop_column("publish_records", "commit_attempted_at")
