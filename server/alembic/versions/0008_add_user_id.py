"""为核心表添加 user_id 外键

修订 ID: 0008
上一修订: 0007
创建日期: 2026-05-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 账号表
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
    op.execute("UPDATE accounts SET user_id = 1")
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.alter_column("user_id", nullable=False, existing_type=sa.Integer())
        batch_op.create_foreign_key("fk_accounts_user_id", "users", ["user_id"], ["id"])
        batch_op.create_index("ix_accounts_user_id", ["user_id"], unique=False)

    # 文章表
    with op.batch_alter_table("articles") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
    op.execute("UPDATE articles SET user_id = 1")
    with op.batch_alter_table("articles") as batch_op:
        batch_op.alter_column("user_id", nullable=False, existing_type=sa.Integer())
        batch_op.create_foreign_key("fk_articles_user_id", "users", ["user_id"], ["id"])
        batch_op.create_index("ix_articles_user_id", ["user_id"], unique=False)

    # 文章分组表
    with op.batch_alter_table("article_groups") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
    op.execute("UPDATE article_groups SET user_id = 1")
    with op.batch_alter_table("article_groups") as batch_op:
        batch_op.alter_column("user_id", nullable=False, existing_type=sa.Integer())
        batch_op.create_foreign_key("fk_article_groups_user_id", "users", ["user_id"], ["id"])
        batch_op.create_index("ix_article_groups_user_id", ["user_id"], unique=False)

    # 发布任务表
    with op.batch_alter_table("publish_tasks") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
    op.execute("UPDATE publish_tasks SET user_id = 1")
    with op.batch_alter_table("publish_tasks") as batch_op:
        batch_op.alter_column("user_id", nullable=False, existing_type=sa.Integer())
        batch_op.create_foreign_key("fk_publish_tasks_user_id", "users", ["user_id"], ["id"])
        batch_op.create_index("ix_publish_tasks_user_id", ["user_id"], unique=False)

    # 资源表
    with op.batch_alter_table("assets") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
    op.execute("UPDATE assets SET user_id = 1")
    with op.batch_alter_table("assets") as batch_op:
        batch_op.alter_column("user_id", nullable=False, existing_type=sa.Integer())
        batch_op.create_foreign_key("fk_assets_user_id", "users", ["user_id"], ["id"])
        batch_op.create_index("ix_assets_user_id", ["user_id"], unique=False)

    # 文章分组表：在 (user_id, name) 上添加用户维度唯一约束
    with op.batch_alter_table("article_groups") as batch_op:
        batch_op.create_unique_constraint("uq_article_groups_user_name", ["user_id", "name"])

    # 账号表：将平台用户唯一约束替换为用户维度版本
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_constraint("uq_accounts_platform_user", type_="unique")
        batch_op.create_unique_constraint("uq_accounts_user_platform_user", ["user_id", "platform_id", "platform_user_id"])


def downgrade() -> None:
    # 账号表：恢复旧唯一约束
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_constraint("uq_accounts_user_platform_user", type_="unique")
        batch_op.create_unique_constraint("uq_accounts_platform_user", ["platform_id", "platform_user_id"])

    # 文章分组表：删除用户维度唯一约束
    with op.batch_alter_table("article_groups") as batch_op:
        batch_op.drop_constraint("uq_article_groups_user_name", type_="unique")

    # 资源表
    with op.batch_alter_table("assets") as batch_op:
        batch_op.drop_index("ix_assets_user_id")
        batch_op.drop_constraint("fk_assets_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    # 发布任务表
    with op.batch_alter_table("publish_tasks") as batch_op:
        batch_op.drop_index("ix_publish_tasks_user_id")
        batch_op.drop_constraint("fk_publish_tasks_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    # 文章分组表
    with op.batch_alter_table("article_groups") as batch_op:
        batch_op.drop_index("ix_article_groups_user_id")
        batch_op.drop_constraint("fk_article_groups_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    # 文章表
    with op.batch_alter_table("articles") as batch_op:
        batch_op.drop_index("ix_articles_user_id")
        batch_op.drop_constraint("fk_articles_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    # 账号表
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_index("ix_accounts_user_id")
        batch_op.drop_constraint("fk_accounts_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")
