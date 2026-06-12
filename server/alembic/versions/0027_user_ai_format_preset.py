"""添加用户 AI 排版预设

修订 ID: 0027
上一修订: 0026
创建日期: 2026-05-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("ai_format_preset_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_users_ai_format_preset_id",
            "prompt_templates",
            ["ai_format_preset_id"],
            ["id"],
        )
        batch_op.create_index("ix_users_ai_format_preset_id", ["ai_format_preset_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index("ix_users_ai_format_preset_id")
        batch_op.drop_constraint("fk_users_ai_format_preset_id", type_="foreignkey")
        batch_op.drop_column("ai_format_preset_id")
