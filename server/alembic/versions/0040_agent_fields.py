"""pipeline 上的智能体管理字段

修订 ID: 0040
上一修订: 0039
创建日期: 2026-06-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("pipelines", sa.Column("type", sa.String(20), nullable=False, server_default="general"))
    op.add_column("pipelines", sa.Column("tags", sa.JSON(), nullable=True))
    op.add_column("pipelines", sa.Column("ignore_exception", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.add_column("pipelines", sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")))
    op.add_column("pipelines", sa.Column("schedule_kind", sa.String(20), nullable=False, server_default="none"))
    op.add_column("pipelines", sa.Column("schedule_minute", sa.Integer(), nullable=True))
    op.add_column("pipelines", sa.Column("schedule_hour", sa.Integer(), nullable=True))
    op.add_column("pipelines", sa.Column("schedule_weekday", sa.Integer(), nullable=True))
    op.add_column("pipelines", sa.Column("window_start", sa.Time(), nullable=True))
    op.add_column("pipelines", sa.Column("window_end", sa.Time(), nullable=True))
    op.add_column("pipelines", sa.Column("last_scheduled_run_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE pipelines SET tags = JSON_ARRAY() WHERE tags IS NULL")


def downgrade() -> None:
    for col in (
        "last_scheduled_run_at", "window_end", "window_start", "schedule_weekday",
        "schedule_hour", "schedule_minute", "schedule_kind", "is_enabled",
        "ignore_exception", "tags", "type",
    ):
        op.drop_column("pipelines", col)
