"""Add account operation ordering timestamp.

Revision ID: 0053
Revises: 0052
Create Date: 2026-06-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("last_operated_at", sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE accounts "
        "SET last_operated_at = COALESCE(updated_at, created_at, UTC_TIMESTAMP()) "
        "WHERE last_operated_at IS NULL"
    )
    op.alter_column(
        "accounts",
        "last_operated_at",
        existing_type=sa.DateTime(),
        nullable=False,
    )
    op.create_index(op.f("ix_accounts_last_operated_at"), "accounts", ["last_operated_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_accounts_last_operated_at"), table_name="accounts")
    op.drop_column("accounts", "last_operated_at")
