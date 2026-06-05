"""pipeline_runs: frozen execution snapshot

Revision ID: 0041
Revises: 0040
Create Date: 2026-06-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("pipeline_runs", sa.Column("snapshot", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("pipeline_runs", "snapshot")
