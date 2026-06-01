"""change client_request_id unique constraints to include user_id

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-20
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    for table, old_name, new_name in [
        ("publish_tasks", "uq_publish_tasks_client_request_id", "uq_publish_tasks_user_client_request_id"),
        ("articles", "uq_articles_client_request_id", "uq_articles_user_client_request_id"),
    ]:
        try:
            op.drop_constraint(old_name, table, type_="unique")
        except Exception:
            pass
        op.create_unique_constraint(new_name, table, ["user_id", "client_request_id"])


def downgrade() -> None:
    for table, old_name, new_name in [
        ("publish_tasks", "uq_publish_tasks_user_client_request_id", "uq_publish_tasks_client_request_id"),
        ("articles", "uq_articles_user_client_request_id", "uq_articles_client_request_id"),
    ]:
        op.drop_constraint(old_name, table, type_="unique")
        op.create_unique_constraint(new_name, table, ["client_request_id"])
