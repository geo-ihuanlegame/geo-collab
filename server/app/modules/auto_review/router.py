"""auto_review router：`POST /api/articles/score` + `POST /api/articles/{id}/auto-review`。

两条都用 MCP token 鉴权（独立于 user JWT）。
注意 prefix 挂在 main.py 是 `/api/articles`，因此本 router path 自己不再带 `/articles`。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from server.app.core.mcp_auth import require_mcp_token
from server.app.db.session import get_db
from server.app.modules.auto_review.schemas import (
    AutoReviewDecisionRead,
    AutoReviewSubmitRequest,
    ScoreRequest,
    ScoreResponse,
)
from server.app.modules.auto_review.service import score_articles, submit_decision

router = APIRouter()


@router.post(
    "/score",
    response_model=ScoreResponse,
    dependencies=[Depends(require_mcp_token)],
)
def post_score(req: ScoreRequest, db: Session = Depends(get_db)) -> ScoreResponse:
    """[MCP] LLM 批量评分。最多 20 篇一次（schema 校验）。"""
    results = score_articles(db, req)
    return ScoreResponse(results=results)


@router.post(
    "/{article_id}/auto-review",
    response_model=AutoReviewDecisionRead,
    dependencies=[Depends(require_mcp_token)],
)
def post_auto_review(
    article_id: int,
    req: AutoReviewSubmitRequest,
    db: Session = Depends(get_db),
) -> AutoReviewDecisionRead:
    """[MCP] 写一条自动审核 decision。"""
    try:
        decision = submit_decision(db, article_id, req)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    db.refresh(decision)
    return AutoReviewDecisionRead.model_validate(decision)
