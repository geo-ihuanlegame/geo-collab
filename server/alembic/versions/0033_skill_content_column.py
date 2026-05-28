"""skills: add content column (single-text refactor)

Revision ID: 0033
Revises: 0032
Create Date: 2026-05-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect


revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if "skills" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("skills")}
    if "content" not in cols:
        op.add_column(
            "skills",
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if "skills" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("skills")}
    if "content" in cols:
        op.drop_column("skills", "content")
