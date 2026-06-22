"""MCP catalog 只读端点（挂在 /api/mcp 下，service token 鉴权）。

复用各模块 service 层，给 Claude Code Loop 的 catalog tools 拉数据。无 per-user 过滤，
MCP 调用按 service 视角返回全量；写操作仍走各模块自己的 MCP sub-router。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from server.app.core.mcp_auth import require_mcp_token
from server.app.db.session import get_db
from server.app.modules.accounts.schemas import AccountRead, to_account_read
from server.app.modules.accounts.service import list_accounts as svc_list_accounts
from server.app.modules.ai_generation import question_bank as qb
from server.app.modules.ai_generation.schemas import (
    QuestionItemRead,
    QuestionPoolRead,
)
from server.app.modules.articles import get_article as svc_get_article
from server.app.modules.articles import list_articles as svc_list_articles
from server.app.modules.articles.schemas import (
    ArticleListRead,
    ArticleRead,
    to_article_read,
)
from server.app.modules.pipelines.models import Pipeline
from server.app.modules.prompt_templates.schemas import PromptScope, PromptTemplateRead
from server.app.modules.prompt_templates.service import list_prompt_templates as svc_list_templates
from server.app.modules.tasks.models import PublishRecord

router = APIRouter(dependencies=[Depends(require_mcp_token)])


# ── articles ───────────────────────────────────────────────────────────────


@router.get("/articles", response_model=list[ArticleListRead])
def mcp_list_articles(
    status: str | None = Query(default=None),
    review_status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[ArticleListRead]:
    """[MCP] 列文章（service 视角，无 per-user 过滤）。"""
    articles = svc_list_articles(
        db,
        query=None,
        skip=0,
        limit=limit,
        user_id=None,
        review_status=review_status,
    )
    if status:
        articles = [a for a in articles if a.status == status]
    if not articles:
        return []
    article_ids = [a.id for a in articles]
    published_counts: dict[int, int] = dict(
        db.execute(
            select(PublishRecord.article_id, func.count().label("cnt"))
            .where(
                PublishRecord.article_id.in_(article_ids),
                PublishRecord.status == "succeeded",
                PublishRecord.is_deleted == False,  # noqa: E712
            )
            .group_by(PublishRecord.article_id)
        ).all()
    )
    return [
        ArticleListRead(
            id=a.id,
            title=a.title,
            author=a.author,
            cover_asset_id=a.cover_asset_id,
            word_count=a.word_count,
            status=a.status,
            version=a.version,
            review_status=a.review_status,
            published_count=published_counts.get(a.id, 0),
            created_at=a.created_at,
            updated_at=a.updated_at,
        )
        for a in articles
    ]


@router.get("/articles/{article_id}", response_model=ArticleRead)
def mcp_get_article(article_id: int, db: Session = Depends(get_db)) -> ArticleRead:
    """[MCP] 取单篇文章详情。"""
    article = svc_get_article(db, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    return to_article_read(article)


# ── question pools ─────────────────────────────────────────────────────────


@router.get("/question-pools", response_model=list[QuestionPoolRead])
def mcp_list_question_pools(db: Session = Depends(get_db)) -> list[QuestionPoolRead]:
    """[MCP] 列所有问题池（全员共享，无过滤）。"""
    pools = qb.list_pools(db)
    return [
        QuestionPoolRead(
            id=p.id,
            name=p.name,
            feishu_app_token=p.feishu_app_token,
            feishu_table_id=p.feishu_table_id,
            last_synced_at=p.last_synced_at,
            created_at=p.created_at,
            pending_count=len(qb.list_items(db, p.id, status="pending")),
        )
        for p in pools
    ]


@router.get(
    "/question-pools/{pool_id}/items",
    response_model=list[QuestionItemRead],
)
def mcp_list_question_items(
    pool_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    category: str | None = Query(default=None),
    status: str = Query(default="pending"),
    db: Session = Depends(get_db),
) -> list[QuestionItemRead]:
    """[MCP] 列指定问题池的问题项。`category` 留空=不过滤；`status="all"` 不过滤状态。"""
    pool = qb.get_pool(db, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="问题池不存在")
    items = qb.list_items(db, pool.id, status=(None if status == "all" else status))
    if category:
        items = [it for it in items if it.category == category]
    return [QuestionItemRead.model_validate(it) for it in items[:limit]]


# ── prompt templates ───────────────────────────────────────────────────────


@router.get("/prompt-templates", response_model=list[PromptTemplateRead])
def mcp_list_prompt_templates(
    scope: PromptScope | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[PromptTemplateRead]:
    """[MCP] 列提示词模板（全量，仅排除软删）。"""
    templates = svc_list_templates(db, scope=scope)
    return [PromptTemplateRead.model_validate(t) for t in templates]


# ── pipelines ──────────────────────────────────────────────────────────────


@router.get("/pipelines")
def mcp_list_pipelines(
    type_filter: str | None = Query(default=None, alias="type"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """[MCP] 列 pipelines（service 视角，无 per-user 过滤）。返回轻量字段。"""
    pipelines = list(db.execute(select(Pipeline).order_by(Pipeline.id.desc())).scalars().all())
    if type_filter:
        pipelines = [p for p in pipelines if (p.type or "general") == type_filter]
    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "type": p.type or "general",
            "is_enabled": p.is_enabled,
            "schedule_kind": p.schedule_kind,
        }
        for p in pipelines
    ]


# ── accounts ───────────────────────────────────────────────────────────────


@router.get("/accounts", response_model=list[AccountRead])
def mcp_list_accounts(
    platform_code: str | None = Query(default=None),
    distribution_enabled: bool | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[AccountRead]:
    """[MCP] 列账号（service 视角）。可按平台 / 分发开关过滤。"""
    accounts = svc_list_accounts(db, q=None)
    if platform_code:
        accounts = [a for a in accounts if a.platform.code == platform_code]
    if distribution_enabled is not None:
        accounts = [a for a in accounts if a.distribution_enabled == distribution_enabled]
    return [to_account_read(a) for a in accounts]
