"""方案与方案运行的 ai_engine 列

修订 ID: 0036
上一修订: 0035
创建日期: 2026-06-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect


revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    # 方案级 AI 引擎（定义）+ 运行时快照（执行）。None / 空 = 用系统默认写作模型。
    if "generation_schemes" in tables:
        if "ai_engine" not in _columns(inspector, "generation_schemes"):
            op.add_column(
                "generation_schemes", sa.Column("ai_engine", sa.String(100), nullable=True)
            )

    if "generation_scheme_runs" in tables:
        if "ai_engine" not in _columns(inspector, "generation_scheme_runs"):
            op.add_column(
                "generation_scheme_runs", sa.Column("ai_engine", sa.String(100), nullable=True)
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    if "generation_scheme_runs" in tables:
        if "ai_engine" in _columns(inspector, "generation_scheme_runs"):
            op.drop_column("generation_scheme_runs", "ai_engine")

    if "generation_schemes" in tables:
        if "ai_engine" in _columns(inspector, "generation_schemes"):
            op.drop_column("generation_schemes", "ai_engine")
