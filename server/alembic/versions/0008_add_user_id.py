"""add user_id foreign key to core tables

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # accounts
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
    op.execute("UPDATE accounts SET user_id = 1")
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.alter_column("user_id", nullable=False, existing_type=sa.Integer())
        batch_op.create_foreign_key("fk_accounts_user_id", "users", ["user_id"], ["id"])
        batch_op.create_index("ix_accounts_user_id", ["user_id"], unique=False)

    # articles
    with op.batch_alter_table("articles") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
    op.execute("UPDATE articles SET user_id = 1")
    with op.batch_alter_table("articles") as batch_op:
        batch_op.alter_column("user_id", nullable=False, existing_type=sa.Integer())
        batch_op.create_foreign_key("fk_articles_user_id", "users", ["user_id"], ["id"])
        batch_op.create_index("ix_articles_user_id", ["user_id"], unique=False)

    # article_groups
    with op.batch_alter_table("article_groups") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
    op.execute("UPDATE article_groups SET user_id = 1")
    with op.batch_alter_table("article_groups") as batch_op:
        batch_op.alter_column("user_id", nullable=False, existing_type=sa.Integer())
        batch_op.create_foreign_key("fk_article_groups_user_id", "users", ["user_id"], ["id"])
        batch_op.create_index("ix_article_groups_user_id", ["user_id"], unique=False)

    # publish_tasks
    with op.batch_alter_table("publish_tasks") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
    op.execute("UPDATE publish_tasks SET user_id = 1")
    with op.batch_alter_table("publish_tasks") as batch_op:
        batch_op.alter_column("user_id", nullable=False, existing_type=sa.Integer())
        batch_op.create_foreign_key("fk_publish_tasks_user_id", "users", ["user_id"], ["id"])
        batch_op.create_index("ix_publish_tasks_user_id", ["user_id"], unique=False)

    # assets
    with op.batch_alter_table("assets") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
    op.execute("UPDATE assets SET user_id = 1")
    with op.batch_alter_table("assets") as batch_op:
        batch_op.alter_column("user_id", nullable=False, existing_type=sa.Integer())
        batch_op.create_foreign_key("fk_assets_user_id", "users", ["user_id"], ["id"])
        batch_op.create_index("ix_assets_user_id", ["user_id"], unique=False)

    # article_groups: user-scoped unique constraint on (user_id, name)
    with op.batch_alter_table("article_groups") as batch_op:
        batch_op.create_unique_constraint("uq_article_groups_user_name", ["user_id", "name"])

    # accounts: replace platform_user unique constraint with user-scoped version
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_constraint("uq_accounts_platform_user", type_="unique")
        batch_op.create_unique_constraint("uq_accounts_user_platform_user", ["user_id", "platform_id", "platform_user_id"])


def downgrade() -> None:
    # accounts: restore old unique constraint
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_constraint("uq_accounts_user_platform_user", type_="unique")
        batch_op.create_unique_constraint("uq_accounts_platform_user", ["platform_id", "platform_user_id"])

    # article_groups: drop user-scoped unique
    with op.batch_alter_table("article_groups") as batch_op:
        batch_op.drop_constraint("uq_article_groups_user_name", type_="unique")

    # assets
    with op.batch_alter_table("assets") as batch_op:
        batch_op.drop_index("ix_assets_user_id")
        batch_op.drop_constraint("fk_assets_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    # publish_tasks
    with op.batch_alter_table("publish_tasks") as batch_op:
        batch_op.drop_index("ix_publish_tasks_user_id")
        batch_op.drop_constraint("fk_publish_tasks_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    # article_groups
    with op.batch_alter_table("article_groups") as batch_op:
        batch_op.drop_index("ix_article_groups_user_id")
        batch_op.drop_constraint("fk_article_groups_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    # articles
    with op.batch_alter_table("articles") as batch_op:
        batch_op.drop_index("ix_articles_user_id")
        batch_op.drop_constraint("fk_articles_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    # accounts
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_index("ix_accounts_user_id")
        batch_op.drop_constraint("fk_accounts_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")
