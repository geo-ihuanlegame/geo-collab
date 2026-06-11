"""账号 app_id 查重改全局唯一：清理存量软删死行 + 唯一约束去掉 user_id

修订 ID: 0045
上一修订: 0044
创建日期: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1) 清理存量死行：释放占位 + 抹密钥（与新 delete_account 行为对齐）
    conn.execute(
        sa.text(
            "UPDATE accounts "
            "SET platform_user_id = NULL, "
            "    api_token_cache = NULL, "
            "    api_credentials = JSON_REMOVE(api_credentials, '$.app_secret') "
            "WHERE is_deleted = 1"
        )
    )

    # 2) 冲突探测：活账号里若已存在跨用户同 (platform_id, app_id)，无法建全局唯一约束 → 中止
    dupes = conn.execute(
        sa.text(
            "SELECT platform_id, platform_user_id, GROUP_CONCAT(id) AS ids "
            "FROM accounts "
            "WHERE is_deleted = 0 AND platform_user_id IS NOT NULL "
            "GROUP BY platform_id, platform_user_id "
            "HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if dupes:
        detail = "; ".join(f"platform_id={r[0]} app_id={r[1]} 重复行 id=[{r[2]}]" for r in dupes)
        raise RuntimeError(
            "迁移中止：存在跨用户重复的活账号 app_id，无法切换为全局唯一，请先人工合并/删除："
            + detail
        )

    # 3) 切换唯一约束：(user_id, platform_id, platform_user_id) → (platform_id, platform_user_id)
    #    旧约束名为 uq_accounts_user_platform_user，新约束名改为 uq_accounts_platform_user
    op.drop_constraint("uq_accounts_user_platform_user", "accounts", type_="unique")
    op.create_unique_constraint(
        "uq_accounts_platform_user", "accounts", ["platform_id", "platform_user_id"]
    )


def downgrade() -> None:
    # 注意：死行 platform_user_id / app_secret 的清理不可逆，仅恢复约束形状。
    op.drop_constraint("uq_accounts_platform_user", "accounts", type_="unique")
    op.create_unique_constraint(
        "uq_accounts_user_platform_user",
        "accounts",
        ["user_id", "platform_id", "platform_user_id"],
    )
