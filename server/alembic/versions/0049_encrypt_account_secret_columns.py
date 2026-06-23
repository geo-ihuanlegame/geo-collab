"""accounts.api_credentials / api_token_cache 改 TEXT（承载 enc:v1: 密文）。

数据加密由幂等脚本 server.scripts.encrypt_secrets 完成，本迁移只改列类型。
JSON→TEXT 后 MySQL 把原值转成 JSON 文本表示，向后兼容读（无 enc: 前缀＝明文）。

修订 ID: 0049
上一修订: 0048
创建日期: 2026-06-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import NoInspectionAvailable

from alembic import context, op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("api_credentials", "api_token_cache")


def _col_type_name(inspector, column: str) -> str:
    for col in inspector.get_columns("accounts"):
        if col["name"] == column:
            return type(col["type"]).__name__.upper()
    return ""


def upgrade() -> None:
    if context.is_offline_mode():
        # --sql 离线模式：直接生成 ALTER TABLE，不做列类型探查
        for column in _COLUMNS:
            op.alter_column(
                "accounts",
                column,
                existing_type=sa.JSON(),
                type_=sa.Text(),
                existing_nullable=True,
            )
        return
    bind = op.get_bind()
    try:
        inspector = sa_inspect(bind)
    except NoInspectionAvailable:
        # 回退：无法探查时直接执行（不做幂等校验）
        for column in _COLUMNS:
            op.alter_column(
                "accounts",
                column,
                existing_type=sa.JSON(),
                type_=sa.Text(),
                existing_nullable=True,
            )
        return
    for column in _COLUMNS:
        if "JSON" in _col_type_name(inspector, column):
            op.alter_column(
                "accounts",
                column,
                existing_type=sa.JSON(),
                type_=sa.Text(),
                existing_nullable=True,
            )


def downgrade() -> None:
    if context.is_offline_mode():
        for column in _COLUMNS:
            op.alter_column(
                "accounts",
                column,
                existing_type=sa.Text(),
                type_=sa.JSON(),
                existing_nullable=True,
            )
        return
    bind = op.get_bind()
    try:
        inspector = sa_inspect(bind)
    except NoInspectionAvailable:
        for column in _COLUMNS:
            op.alter_column(
                "accounts",
                column,
                existing_type=sa.Text(),
                type_=sa.JSON(),
                existing_nullable=True,
            )
        return
    for column in _COLUMNS:
        if "JSON" not in _col_type_name(inspector, column):
            op.alter_column(
                "accounts",
                column,
                existing_type=sa.Text(),
                type_=sa.JSON(),
                existing_nullable=True,
            )
