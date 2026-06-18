"""account_dedup_sharing：头条账号 creator-ID 查重 + 共享账号基建

修订 ID: 0047
上一修订: 0046
创建日期: 2026-06-18

落地（见设计稿 2026-06-17-toutiao-account-dedup-sharing-design.md §2.7）：
  - 新表 account_members（共享账号成员表，复合 PK (account_id, user_id)）。
  - accounts 加 merged_into（合并 tombstone 自引用 FK，nullable, index）。
  - account_login_sessions 加 resolved_account_id / extracted_platform_user_id 结果列。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)

    # 1) 新表 account_members
    if "account_members" not in inspector.get_table_names():
        op.create_table(
            "account_members",
            sa.Column(
                "account_id",
                sa.Integer(),
                sa.ForeignKey("accounts.id", ondelete="CASCADE"),
                primary_key=True,
                nullable=False,
            ),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                primary_key=True,
                nullable=False,
            ),
            sa.Column("granted_via", sa.String(40), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )

    # 2) accounts.merged_into（自引用 FK + 索引）
    if not _has_column(inspector, "accounts", "merged_into"):
        op.add_column("accounts", sa.Column("merged_into", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_accounts_merged_into",
            "accounts",
            "accounts",
            ["merged_into"],
            ["id"],
        )
        op.create_index(op.f("ix_accounts_merged_into"), "accounts", ["merged_into"])

    # 3) account_login_sessions 结果列
    if not _has_column(inspector, "account_login_sessions", "resolved_account_id"):
        op.add_column(
            "account_login_sessions",
            sa.Column("resolved_account_id", sa.Integer(), nullable=True),
        )
    if not _has_column(inspector, "account_login_sessions", "extracted_platform_user_id"):
        op.add_column(
            "account_login_sessions",
            sa.Column("extracted_platform_user_id", sa.String(200), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)

    if _has_column(inspector, "account_login_sessions", "extracted_platform_user_id"):
        op.drop_column("account_login_sessions", "extracted_platform_user_id")
    if _has_column(inspector, "account_login_sessions", "resolved_account_id"):
        op.drop_column("account_login_sessions", "resolved_account_id")

    if _has_column(inspector, "accounts", "merged_into"):
        # 先删 FK，再删它依赖的索引（否则 MySQL 拒绝 DROP INDEX：1553）。
        op.drop_constraint("fk_accounts_merged_into", "accounts", type_="foreignkey")
        op.drop_index(op.f("ix_accounts_merged_into"), table_name="accounts")
        op.drop_column("accounts", "merged_into")

    if "account_members" in inspector.get_table_names():
        op.drop_table("account_members")
