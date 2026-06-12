"""pipelines：子表外键 ON DELETE CASCADE 与唯一 (pipeline_id, version_no)

修订 ID: 0039
上一修订: 0038
创建日期: 2026-06-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHILDREN = ["pipeline_nodes", "pipeline_versions", "pipeline_runs"]


def _drop_fk_to(table: str, ref_table: str) -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t "
            "AND REFERENCED_TABLE_NAME = :r"
        ),
        {"t": table, "r": ref_table},
    ).fetchall()
    for (name,) in rows:
        op.drop_constraint(name, table, type_="foreignkey")


def upgrade() -> None:
    # 1) (pipeline_id, version_no) 普通索引 → 唯一。
    #    必须先建新唯一索引、再删旧索引：Alembic 元数据里 pipeline_id 外键依赖
    #    ix_pipeline_versions_pipeline_version 作为覆盖索引（pipeline_id 是左前缀），
    #    新唯一索引同样覆盖 pipeline_id，建好后旧索引才允许被删除（否则 MySQL 1553）。
    op.create_index(
        "uq_pipeline_versions_pipeline_version",
        "pipeline_versions",
        ["pipeline_id", "version_no"],
        unique=True,
    )
    op.drop_index("ix_pipeline_versions_pipeline_version", table_name="pipeline_versions")
    # 2) 子表 → pipelines 外键改 CASCADE（先删旧匿名 FK，再建命名 FK）
    for child in _CHILDREN:
        _drop_fk_to(child, "pipelines")
        op.create_foreign_key(
            f"fk_{child}_pipeline",
            child,
            "pipelines",
            ["pipeline_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    for child in _CHILDREN:
        op.drop_constraint(f"fk_{child}_pipeline", child, type_="foreignkey")
        op.create_foreign_key(None, child, "pipelines", ["pipeline_id"], ["id"])
    # 同理：先建回旧索引再删唯一索引，保证外键始终有覆盖索引
    op.create_index(
        "ix_pipeline_versions_pipeline_version",
        "pipeline_versions",
        ["pipeline_id", "version_no"],
    )
    op.drop_index("uq_pipeline_versions_pipeline_version", table_name="pipeline_versions")
