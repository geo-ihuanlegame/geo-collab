"""auto_review service：LLM 批量评分 + decision 持久化。

评分用 ai_format_model（deepseek-v4-flash 经济档），由 ai_models.service 解析。
失败容错：单条评分失败 → score_total=None + reasoning="[评分失败] ..." 仍入结果列表。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from server.app.modules.articles.models import Article
from server.app.modules.auto_review.models import AutoReviewDecision
from server.app.modules.auto_review.schemas import (
    AutoReviewSubmitRequest,
    ScoreBreakdown,
    ScoreRequest,
)

DEFAULT_DIMENSIONS = ["factuality", "readability", "style", "policy_safety"]


def score_articles(db: Session, req: ScoreRequest) -> list[ScoreBreakdown]:
    """批量评分。每条独立调 LLM，单条失败不影响其它。返回结果与 input 顺序一致。"""
    # Task 16 实现
    raise NotImplementedError("Task 16")


def submit_decision(
    db: Session,
    article_id: int,
    req: AutoReviewSubmitRequest,
) -> AutoReviewDecision:
    """写一条 AutoReviewDecision。注意：不动 article.review_status，最终人审兜底。"""
    article = db.query(Article).filter(Article.id == article_id).first()
    if article is None:
        raise ValueError(f"article not found: {article_id}")
    decision = AutoReviewDecision(
        article_id=article_id,
        decision=req.decision,
        score_total=req.score_total,
        score_breakdown=req.score_breakdown,
        reasoning=req.reasoning,
        decided_by=req.decided_by,
    )
    db.add(decision)
    db.flush()
    return decision
