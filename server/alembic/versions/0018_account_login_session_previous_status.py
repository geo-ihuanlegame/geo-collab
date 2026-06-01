"""account_login_sessions: add previous_status to restore on cancel

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("account_login_sessions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("previous_status", sa.String(length=30), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("account_login_sessions", schema=None) as batch_op:
        batch_op.drop_column("previous_status")
