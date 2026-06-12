"""账号表支持 API 型平台（微信公众号）：凭据/token 缓存/分发开关/联系方式/头像；种入 wechat_mp 平台

修订 ID: 0044
上一修订: 0043
创建日期: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("api_credentials", sa.JSON(), nullable=True))
    op.add_column("accounts", sa.Column("api_token_cache", sa.JSON(), nullable=True))
    op.add_column(
        "accounts",
        sa.Column("distribution_enabled", sa.Boolean(), nullable=False, server_default="1"),
    )
    op.create_index("ix_accounts_distribution_enabled", "accounts", ["distribution_enabled"])
    op.add_column("accounts", sa.Column("contact", sa.String(200), nullable=True))
    op.add_column("accounts", sa.Column("avatar_asset_id", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_accounts_avatar_asset_id", "accounts", "assets", ["avatar_asset_id"], ["id"]
    )
    op.alter_column("accounts", "state_path", existing_type=sa.String(1000), nullable=True)
    # 幂等种入 wechat_mp 平台
    conn = op.get_bind()
    exists = conn.execute(sa.text("SELECT id FROM platforms WHERE code = 'wechat_mp'")).first()
    if exists is None:
        conn.execute(
            sa.text(
                "INSERT INTO platforms (code, name, base_url, enabled, created_at) "
                "VALUES ('wechat_mp', '微信公众号', 'https://mp.weixin.qq.com', 1, NOW())"
            )
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM platforms WHERE code = 'wechat_mp'"))
    op.alter_column("accounts", "state_path", existing_type=sa.String(1000), nullable=False)
    op.drop_constraint("fk_accounts_avatar_asset_id", "accounts", type_="foreignkey")
    op.drop_column("accounts", "avatar_asset_id")
    op.drop_column("accounts", "contact")
    op.drop_index("ix_accounts_distribution_enabled", table_name="accounts")
    op.drop_column("accounts", "distribution_enabled")
    op.drop_column("accounts", "api_token_cache")
    op.drop_column("accounts", "api_credentials")
