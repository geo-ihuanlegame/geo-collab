"""全文索引和复合索引

修订 ID: 0007
上一修订: 0006
创建日期: 2026-05-12
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _require_mysql() -> None:
    bind = op.get_bind()
    if bind.engine.dialect.name != "mysql":
        raise RuntimeError("Geo Collab migrations require MySQL")


def upgrade() -> None:
    _require_mysql()
    op.execute(
        "ALTER TABLE articles ADD FULLTEXT INDEX ft_articles (title, author) WITH PARSER ngram"
    )

    op.create_index("ix_accounts_platform_status", "accounts", ["platform_id", "status"])
    op.create_index("ix_publish_records_task_status", "publish_records", ["task_id", "status"])
    op.create_index("ix_publish_records_account_status", "publish_records", ["account_id", "status"])


def downgrade() -> None:
    _require_mysql()

    op.drop_index("ix_publish_records_account_status", table_name="publish_records")
    op.drop_index("ix_publish_records_task_status", table_name="publish_records")
    op.drop_index("ix_accounts_platform_status", table_name="accounts")
    op.execute("ALTER TABLE articles DROP INDEX ft_articles")
