"""tasks: add article_round_robin to task_type check constraint

Revision ID: 0042
Revises: 0041
Create Date: 2026-06-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_publish_tasks_task_type", "publish_tasks", type_="check")
    op.create_check_constraint(
        "ck_publish_tasks_task_type",
        "publish_tasks",
        "task_type in ('single', 'group_round_robin', 'article_round_robin')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_publish_tasks_task_type", "publish_tasks", type_="check")
    op.create_check_constraint(
        "ck_publish_tasks_task_type",
        "publish_tasks",
        "task_type in ('single', 'group_round_robin')",
    )
