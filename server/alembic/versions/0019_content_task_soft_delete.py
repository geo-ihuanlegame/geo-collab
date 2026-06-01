"""content/tasks: add soft delete columns

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SOFT_DELETE_TABLES = (
    "articles",
    "article_groups",
    "publish_tasks",
    "publish_records",
)


def upgrade() -> None:
    for table_name in SOFT_DELETE_TABLES:
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="0")
            )
            batch_op.add_column(
                sa.Column("deleted_at", sa.DateTime(), nullable=True)
            )
            batch_op.create_index(f"ix_{table_name}_is_deleted", ["is_deleted"])


def downgrade() -> None:
    for table_name in reversed(SOFT_DELETE_TABLES):
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.drop_index(f"ix_{table_name}_is_deleted")
            batch_op.drop_column("deleted_at")
            batch_op.drop_column("is_deleted")
