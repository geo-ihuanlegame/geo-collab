from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user, require_admin
from server.app.db.session import get_db
from server.app.models import Article, PublishRecord, User
from server.app.schemas.article import ArticleCoverUpdate, ArticleCreate, ArticleListRead, ArticleRead, ArticleUpdate
from server.app.shared.errors import ConflictError
from server.app.modules.articles import (
    create_article,
    delete_article,
    get_article,
    list_articles,
    set_article_cover,
    update_article,
)
from server.app.api.serializers import to_article_read

router = APIRouter()


def _verify_article_ownership(article: Article | None, current_user: User) -> Article:
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    if current_user.role != "admin" and article.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Article not found")
    return article


# 获取文章列表，支持按标题/作者搜索、分页
@router.get("", response_model=list[ArticleListRead])
def read_articles(
    q: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ArticleListRead]:
    articles = list_articles(db, q, skip=skip, limit=limit, user_id=None if current_user.role == "admin" else current_user.id)
    if not articles:
        return []
    article_ids = [a.id for a in articles]
    rows = db.execute(
        select(PublishRecord.article_id, func.count().label("cnt"))
        .where(PublishRecord.article_id.in_(article_ids), PublishRecord.status == "succeeded")
        .group_by(PublishRecord.article_id)
    ).all()
    count_map = {row.article_id: row.cnt for row in rows}
    return [
        ArticleListRead(
            id=a.id,
            title=a.title,
            author=a.author,
            cover_asset_id=a.cover_asset_id,
            word_count=a.word_count,
            status=a.status,
            version=a.version,
            published_count=count_map.get(a.id, 0),
            created_at=a.created_at,
            updated_at=a.updated_at,
        )
        for a in articles
    ]


# 创建新文章
@router.post("", response_model=ArticleRead)
def create_article_endpoint(
    payload: ArticleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleRead:
    try:
        return to_article_read(create_article(db, current_user.id, payload))
    except IntegrityError as exc:
        db.rollback()
        if payload.client_request_id:
            existing = db.execute(
                select(Article).where(
                    Article.client_request_id == payload.client_request_id,
                    Article.user_id == current_user.id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return to_article_read(get_article(db, existing.id) or existing)
        raise exc


# 获取单篇文章详情
@router.get("/{article_id}", response_model=ArticleRead)
def read_article(
    article_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleRead:
    article = _verify_article_ownership(get_article(db, article_id), current_user)
    return to_article_read(article)


# 更新文章内容（标题、正文、封面等）
@router.put("/{article_id}", response_model=ArticleRead)
def update_article_endpoint(
    article_id: int,
    payload: ArticleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleRead:
    article = _verify_article_ownership(get_article(db, article_id), current_user)
    return to_article_read(update_article(db, article, payload))


# 删除文章
@router.delete("/{article_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_article_endpoint(
    article_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Response:
    article = _verify_article_ownership(get_article(db, article_id), current_user)
    delete_article(db, article)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 仅更新文章封面图
@router.post("/{article_id}/cover", response_model=ArticleRead)
def update_article_cover(
    article_id: int,
    payload: ArticleCoverUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleRead:
    article = _verify_article_ownership(get_article(db, article_id), current_user)
    if payload.version is not None and article.version != payload.version:
        raise ConflictError("Article has been modified; refresh before saving")
    return to_article_read(set_article_cover(db, article, payload.cover_asset_id))

