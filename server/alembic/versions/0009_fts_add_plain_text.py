"""将 plain_text 加入文章全文搜索索引

修订 ID: 0009
上一修订: 0008
创建日期: 2026-05-13
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _require_mysql() -> None:
    bind = op.get_bind()
    if bind.engine.dialect.name != "mysql":
        raise RuntimeError("Geo Collab migrations require MySQL")


def upgrade() -> None:
    _require_mysql()
    op.execute("ALTER TABLE articles DROP INDEX ft_articles")
    op.execute(
        "ALTER TABLE articles ADD FULLTEXT INDEX ft_articles (title, author, plain_text) WITH PARSER ngram"
    )


def downgrade() -> None:
    _require_mysql()
    op.execute("ALTER TABLE articles DROP INDEX ft_articles")
    op.execute(
        "ALTER TABLE articles ADD FULLTEXT INDEX ft_articles (title, author) WITH PARSER ngram"
    )
