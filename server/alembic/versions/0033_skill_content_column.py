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
        # MySQL 不允许 TEXT 列有字面 DEFAULT（错误 1101）。
        # 分三步：先加 nullable 列，回填空串，再改为 NOT NULL。
        op.add_column("skills", sa.Column("content", sa.Text(), nullable=True))
        op.execute("UPDATE skills SET content = '' WHERE content IS NULL")
        op.alter_column("skills", "content", existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if "skills" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("skills")}
    if "content" in cols:
        op.drop_column("skills", "content")
