"""ai_models：AI 模型注册表（写作 + 格式·配图，DB 化管理，密钥不入库）

修订 ID: 0046
上一修订: 0045
创建日期: 2026-06-17

uq_ai_models_scope_default(scope, is_default_key)：is_default_key 在 is_default=True
时 = scope、否则 NULL，借 MySQL「唯一索引允许多 NULL、拒重复非 NULL」实现每 scope
至多一个默认模型的 DB 级硬约束。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if "ai_models" in inspector.get_table_names():
        return

    op.create_table(
        "ai_models",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("scope", sa.String(20), nullable=False),
        sa.Column("base_url", sa.String(300), nullable=True),
        sa.Column("api_key_env", sa.String(80), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("is_default_key", sa.String(20), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_index(op.f("ix_ai_models_scope"), "ai_models", ["scope"])
    op.create_index("ix_ai_models_scope_enabled", "ai_models", ["scope", "is_enabled"])
    op.create_unique_constraint(
        "uq_ai_models_scope_default", "ai_models", ["scope", "is_default_key"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if "ai_models" not in inspector.get_table_names():
        return

    op.drop_constraint("uq_ai_models_scope_default", "ai_models", type_="unique")
    op.drop_index("ix_ai_models_scope_enabled", table_name="ai_models")
    op.drop_index(op.f("ix_ai_models_scope"), table_name="ai_models")
    op.drop_table("ai_models")
