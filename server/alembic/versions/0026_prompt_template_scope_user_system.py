"""为 prompt_templates 添加 scope、owner 和 system 标记

修订 ID: 0026
上一修订: 0025
创建日期: 2026-05-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("prompt_templates", schema=None) as batch_op:
        batch_op.add_column(sa.Column("scope", sa.String(length=50), nullable=False, server_default="generation"))
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("is_system", sa.Boolean(), nullable=False, server_default="0"))
        batch_op.create_foreign_key("fk_prompt_templates_user_id", "users", ["user_id"], ["id"])
        batch_op.create_index("ix_prompt_templates_scope", ["scope"], unique=False)
        batch_op.create_index("ix_prompt_templates_user_id", ["user_id"], unique=False)

    op.execute("UPDATE prompt_templates SET scope = 'generation', is_system = 1 WHERE is_deleted = 0")


def downgrade() -> None:
    with op.batch_alter_table("prompt_templates", schema=None) as batch_op:
        batch_op.drop_index("ix_prompt_templates_user_id")
        batch_op.drop_index("ix_prompt_templates_scope")
        batch_op.drop_constraint("fk_prompt_templates_user_id", type_="foreignkey")
        batch_op.drop_column("is_system")
        batch_op.drop_column("user_id")
        batch_op.drop_column("scope")
