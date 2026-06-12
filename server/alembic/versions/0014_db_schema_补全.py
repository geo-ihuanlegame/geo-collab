"""数据库结构补全：solo_mode、scheduled_at、snapshots、tags

修订 ID: 0014
上一修订: 0013
创建日期: 2026-05-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("publish_tasks", schema=None) as batch_op:
        batch_op.alter_column(
            "platform_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch_op.add_column(sa.Column("scheduled_at", sa.DateTime(), nullable=True))

    op.add_column(
        "users",
        sa.Column("solo_mode", sa.Boolean(), nullable=False, server_default="0"),
    )

    op.add_column("publish_records", sa.Column("snapshot_title", sa.String(300), nullable=True))
    op.add_column("publish_records", sa.Column("snapshot_content_json", sa.Text(), nullable=True))

    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tags_name", "tags", ["name"], unique=True)

    op.create_table(
        "article_tags",
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["tags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("article_id", "tag_id"),
    )
    op.create_index("ix_article_tags_tag_id", "article_tags", ["tag_id"])


def downgrade() -> None:
    op.drop_index("ix_article_tags_tag_id", table_name="article_tags")
    op.drop_table("article_tags")
    op.drop_index("ix_tags_name", table_name="tags")
    op.drop_table("tags")
    op.drop_column("publish_records", "snapshot_content_json")
    op.drop_column("publish_records", "snapshot_title")
    op.drop_column("users", "solo_mode")

    with op.batch_alter_table("publish_tasks", schema=None) as batch_op:
        batch_op.drop_column("scheduled_at")
        batch_op.alter_column(
            "platform_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
