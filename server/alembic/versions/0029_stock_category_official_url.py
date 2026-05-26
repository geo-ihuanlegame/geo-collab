"""add official url to stock categories

Revision ID: 0029
Revises: 0028
Create Date: 2026-05-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("stock_categories")}
    if "official_url" in existing:
        return
    with op.batch_alter_table("stock_categories", schema=None) as batch_op:
        batch_op.add_column(sa.Column("official_url", sa.String(length=1000), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("stock_categories")}
    if "official_url" not in existing:
        return
    with op.batch_alter_table("stock_categories", schema=None) as batch_op:
        batch_op.drop_column("official_url")
