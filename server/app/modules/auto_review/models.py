"""AutoReviewDecision — Loop 自评分记录。

跟 articles 是多对一：一个 article 可有多次自动审核记录（每次 Loop 跑都写一条）。
不直接修改 articles.review_status；最终人工审核仍是 truth。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from server.app.core.time import utcnow
from server.app.db.base import Base


class AutoReviewDecision(Base):
    __tablename__ = "auto_review_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    # values: "approved" | "needs_rewrite" | "rejected"

    score_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_breakdown: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    decided_by: Mapped[str] = mapped_column(String(50), nullable=False)
    # 示例: "claude-code-loop" / "auto-reviewer-v1" / "claude-code-manual"

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
