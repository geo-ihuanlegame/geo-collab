"""auto_review router：`POST /api/articles/score` + `POST /api/articles/{id}/auto-review`。

两条都用 MCP token 鉴权（独立于 user JWT）。
注意 prefix 挂在 main.py 是 `/api/articles`，因此本 router path 自己不再带 `/articles`。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from server.app.core.mcp_auth import require_mcp_token
from server.app.core.mcp_errors import mcp_exception_response
from server.app.db.session import get_db
from server.app.modules.auto_review.schemas import (
    AutoReviewDecisionRead,
    AutoReviewSubmitRequest,
    ScoreRequest,
    ScoreResponse,
)
from server.app.modules.auto_review.service import score_articles, submit_decision
from server.app.shared.errors import ClientError, ConflictError, ValidationError

router = APIRouter()


@router.post(
    "/score",
    response_model=ScoreResponse,
    dependencies=[Depends(require_mcp_token)],
)
def post_score(req: ScoreRequest, db: Session = Depends(get_db)) -> ScoreResponse:
    """[MCP] LLM 批量评分。最多 20 篇一次（schema 校验）。

    score_articles 内部对单条 LLM 失败已做容错（标记为 error 行、不传染其它行），
    但 schema/DB 层失败仍可能冒到本端点——用 mcp_exception_response 兜底，
    避免被全局 500 handler 抹成 "服务器内部错误"。
    """
    try:
        results = score_articles(db, req)
    except HTTPException:
        raise
    except (ConflictError, ClientError, ValidationError):
        raise
    except Exception as exc:
        raise mcp_exception_response(
            exc,
            context=f"score_articles ids={req.article_ids[:5]}{'...' if len(req.article_ids) > 5 else ''}",
        ) from exc
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
    except HTTPException:
        raise
    except (ConflictError, ClientError, ValidationError):
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise mcp_exception_response(
            exc,
            context=f"submit_decision article_id={article_id}",
        ) from exc
    db.commit()
    db.refresh(decision)
    return AutoReviewDecisionRead.model_validate(decision)


@router.get(
    "/today-loop-decisions",
    dependencies=[Depends(require_mcp_token)],
)
def get_today_loop_decisions(
    decided_by: str = "claude-goal-verifier",
    decision: str = "approved",
    since_hours: int = Query(24, ge=1, le=168),
    model_label: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """[MCP] /goal loop 的净产出验证查询。

    返回滚动时间窗内 decided_by + decision 命中的 AutoReviewDecision 行，
    join Article 拿 title，可选按 Article.metrics.writer_model 进一步过滤。

    主要消费方：`/goal` orchestrator 每轮调用一次决定是否继续循环。
    """
    from server.app.modules.auto_review.service import list_recent_decisions

    try:
        count, items = list_recent_decisions(
            db,
            decided_by=decided_by,
            decision=decision,
            since_hours=since_hours,
            model_label=model_label,
            limit=limit,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise mcp_exception_response(
            exc,
            context=f"list_today_loop_articles decided_by={decided_by} decision={decision}",
        ) from exc
    return {"ok": True, "data": {"count": count, "items": items}, "error": None}
