"""问题池镜像列与方案池 / 方案运行表

修订 ID: 0035
上一修订: 0034
创建日期: 2026-06-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect


revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    # ── 1. 问题池补列（镜像化）──
    if "question_pools" in tables:
        pool_cols = _columns(inspector, "question_pools")
        if "last_sync_error" not in pool_cols:
            op.add_column("question_pools", sa.Column("last_sync_error", sa.Text(), nullable=True))
        if "auto_sync_enabled" not in pool_cols:
            op.add_column(
                "question_pools",
                sa.Column(
                    "auto_sync_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default="1",
                ),
            )

    # ── 2. 问题项补列（镜像状态）──
    if "question_items" in tables:
        item_cols = _columns(inspector, "question_items")
        if "source_active" not in item_cols:
            op.add_column(
                "question_items",
                sa.Column(
                    "source_active",
                    sa.Boolean(),
                    nullable=False,
                    server_default="1",
                ),
            )
            op.create_index(
                op.f("ix_question_items_source_active"), "question_items", ["source_active"]
            )
        if "source_deleted_at" not in item_cols:
            op.add_column(
                "question_items", sa.Column("source_deleted_at", sa.DateTime(), nullable=True)
            )
        if "last_seen_at" not in item_cols:
            op.add_column("question_items", sa.Column("last_seen_at", sa.DateTime(), nullable=True))

    # ── 3. 方案头 ──
    if "generation_schemes" not in tables:
        op.create_table(
            "generation_schemes",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("pool_id", sa.Integer(), sa.ForeignKey("question_pools.id"), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index(op.f("ix_generation_schemes_user_id"), "generation_schemes", ["user_id"])
        op.create_index(op.f("ix_generation_schemes_pool_id"), "generation_schemes", ["pool_id"])
        op.create_index(
            op.f("ix_generation_schemes_is_deleted"), "generation_schemes", ["is_deleted"]
        )

    # ── 4. 方案行 ──
    if "generation_scheme_lines" not in tables:
        op.create_table(
            "generation_scheme_lines",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "scheme_id",
                sa.Integer(),
                sa.ForeignKey("generation_schemes.id"),
                nullable=False,
            ),
            sa.Column("question_type", sa.String(200), nullable=True),
            sa.Column("article_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("allowed_prompt_template_ids", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            op.f("ix_generation_scheme_lines_scheme_id"),
            "generation_scheme_lines",
            ["scheme_id"],
        )

    # ── 5. 方案行问题（外键 + 快照）──
    if "generation_scheme_line_questions" not in tables:
        op.create_table(
            "generation_scheme_line_questions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "scheme_line_id",
                sa.Integer(),
                sa.ForeignKey("generation_scheme_lines.id"),
                nullable=False,
            ),
            sa.Column(
                "question_item_id",
                sa.Integer(),
                sa.ForeignKey("question_items.id"),
                nullable=True,
            ),
            sa.Column("record_id", sa.String(255), nullable=True),
            sa.Column("question_text", sa.Text(), nullable=True),
            sa.Column("question_type", sa.String(200), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            op.f("ix_generation_scheme_line_questions_scheme_line_id"),
            "generation_scheme_line_questions",
            ["scheme_line_id"],
        )

    # ── 6. 方案运行头 ──
    if "generation_scheme_runs" not in tables:
        op.create_table(
            "generation_scheme_runs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "scheme_id",
                sa.Integer(),
                sa.ForeignKey("generation_schemes.id"),
                nullable=False,
            ),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("article_ids", sa.JSON(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            op.f("ix_generation_scheme_runs_scheme_id"), "generation_scheme_runs", ["scheme_id"]
        )
        op.create_index(
            op.f("ix_generation_scheme_runs_user_id"), "generation_scheme_runs", ["user_id"]
        )
        op.create_index(
            op.f("ix_generation_scheme_runs_status"), "generation_scheme_runs", ["status"]
        )

    # ── 7. 方案运行明细 ──
    if "generation_scheme_run_tasks" not in tables:
        op.create_table(
            "generation_scheme_run_tasks",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "run_id",
                sa.Integer(),
                sa.ForeignKey("generation_scheme_runs.id"),
                nullable=False,
            ),
            sa.Column(
                "scheme_line_id",
                sa.Integer(),
                sa.ForeignKey("generation_scheme_lines.id"),
                nullable=True,
            ),
            sa.Column("question_type", sa.String(200), nullable=True),
            sa.Column("question_text", sa.Text(), nullable=True),
            sa.Column("question_item_ids", sa.JSON(), nullable=True),
            sa.Column("allowed_prompt_template_ids", sa.JSON(), nullable=True),
            sa.Column(
                "actual_prompt_template_id",
                sa.Integer(),
                sa.ForeignKey("prompt_templates.id"),
                nullable=True,
            ),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id"), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            op.f("ix_generation_scheme_run_tasks_run_id"),
            "generation_scheme_run_tasks",
            ["run_id"],
        )
        op.create_index(
            op.f("ix_generation_scheme_run_tasks_status"),
            "generation_scheme_run_tasks",
            ["status"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    for table in (
        "generation_scheme_run_tasks",
        "generation_scheme_runs",
        "generation_scheme_line_questions",
        "generation_scheme_lines",
        "generation_schemes",
    ):
        if table in tables:
            op.drop_table(table)

    if "question_items" in tables:
        item_cols = _columns(inspector, "question_items")
        if "source_active" in item_cols:
            op.drop_index(op.f("ix_question_items_source_active"), table_name="question_items")
            op.drop_column("question_items", "source_active")
        if "source_deleted_at" in item_cols:
            op.drop_column("question_items", "source_deleted_at")
        if "last_seen_at" in item_cols:
            op.drop_column("question_items", "last_seen_at")

    if "question_pools" in tables:
        pool_cols = _columns(inspector, "question_pools")
        if "auto_sync_enabled" in pool_cols:
            op.drop_column("question_pools", "auto_sync_enabled")
        if "last_sync_error" in pool_cols:
            op.drop_column("question_pools", "last_sync_error")
