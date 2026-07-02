"""Add article source template id.

Revision ID: 0054
Revises: 0053
Create Date: 2026-07-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("articles", sa.Column("source_template_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_articles_source_template_id"), "articles", ["source_template_id"])
    op.execute(
        """
        UPDATE articles a
        SET source_template_id = (
            SELECT p.id
            FROM prompt_templates p
            WHERE p.name = a.source_template_name
              AND p.scope = 'generation'
              AND p.is_deleted = 0
              AND (p.user_id = a.user_id OR p.user_id IS NULL)
            ORDER BY CASE WHEN p.user_id = a.user_id THEN 0 ELSE 1 END, p.id
            LIMIT 1
        )
        WHERE a.source_template_id IS NULL
          AND a.source_template_name IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_articles_source_template_id"), table_name="articles")
    op.drop_column("articles", "source_template_id")
