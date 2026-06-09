"""添加 skills、prompt_templates、generation_sessions 表

修订 ID: 0022
上一修订: 0021
创建日期: 2026-05-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("storage_path", sa.String(length=500), nullable=False),
        sa.Column("file_stats", sa.Text(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index(op.f("ix_skills_is_deleted"), "skills", ["is_deleted"], unique=False)

    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index(
        op.f("ix_prompt_templates_is_deleted"), "prompt_templates", ["is_deleted"], unique=False
    )

    op.create_table(
        "generation_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("skill_id", sa.Integer(), nullable=True),
        sa.Column("prompt_template_id", sa.Integer(), nullable=True),
        sa.Column("extra_instruction", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        # MySQL 不允许 TEXT/BLOB/JSON 列有字面 DEFAULT（错误 1101）。
        # 不设 server_default：CREATE TABLE 不插行，NOT NULL 合法；写入由 ORM 端 default="[]" 保证。
        sa.Column("article_ids", sa.Text(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status in ('pending','running','done','failed')",
            name="ck_gen_sessions_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"]),
        sa.ForeignKeyConstraint(["prompt_template_id"], ["prompt_templates.id"]),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index(
        op.f("ix_generation_sessions_user_id"), "generation_sessions", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_generation_sessions_status"), "generation_sessions", ["status"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_generation_sessions_status"), table_name="generation_sessions")
    op.drop_index(op.f("ix_generation_sessions_user_id"), table_name="generation_sessions")
    op.drop_table("generation_sessions")

    op.drop_index(op.f("ix_prompt_templates_is_deleted"), table_name="prompt_templates")
    op.drop_table("prompt_templates")

    op.drop_index(op.f("ix_skills_is_deleted"), table_name="skills")
    op.drop_table("skills")
