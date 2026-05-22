"""add ai_checking lock fields to articles

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "articles",
        sa.Column("ai_checking", sa.Boolean(), server_default="0", nullable=False),
    )
    op.add_column(
        "articles",
        sa.Column("ai_checking_started_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("articles", "ai_checking_started_at")
    op.drop_column("articles", "ai_checking")
