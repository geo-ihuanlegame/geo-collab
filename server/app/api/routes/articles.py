import logging
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from server.app.core.config import get_settings
from server.app.core.security import get_current_user, require_admin
from server.app.db.session import get_db
from server.app.models import Article, PublishRecord, User
from server.app.schemas.article import ArticleCoverUpdate, ArticleCreate, ArticleListRead, ArticleRead, ArticleUpdate
from server.app.shared.errors import ClientError, ConflictError
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
        raise HTTPException(status_code=404, detail="文章不存在")
    if current_user.role != "admin" and article.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="文章不存在")
    return article


def _is_ai_lock_expired(article: Article) -> bool:
    if not article.ai_checking:
        return False
    started = article.ai_checking_started_at
    if started is None:
        return True
    elapsed = (datetime.now(timezone.utc).replace(tzinfo=None) - started).total_seconds()
    return elapsed >= get_settings().ai_format_timeout_seconds


def _clear_ai_lock_if_expired(db: Session, article: Article) -> None:
    if not _is_ai_lock_expired(article):
        return
    article.ai_checking = False
    article.ai_checking_started_at = None
    article.ai_format_error = "AI 排版超时：模型服务响应超时或后台任务未完成，请重试。"
    db.commit()
    db.refresh(article)


def _check_not_ai_locked(db: Session, article: Article) -> None:
    """Raise ConflictError if article is under active AI format check."""
    _clear_ai_lock_if_expired(db, article)
    if not article.ai_checking:
        return
    raise ConflictError("\u6587\u7ae0\u6b63\u5728\u8fdb\u884c AI \u683c\u5f0f\u8c03\u6574\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5")


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
        .where(
            PublishRecord.article_id.in_(article_ids),
            PublishRecord.status == "succeeded",
            PublishRecord.is_deleted == False,  # noqa: E712
        )
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
                    Article.is_deleted == False,  # noqa: E712
                )
            ).scalar_one_or_none()
            if existing is not None:
                return to_article_read(get_article(db, existing.id) or existing)
            raise HTTPException(status_code=409, detail="请求冲突：client_request_id 已存在或数据异常")


# 获取单篇文章详情
@router.get("/{article_id}", response_model=ArticleRead)
def read_article(
    article_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleRead:
    article = _verify_article_ownership(get_article(db, article_id), current_user)
    _clear_ai_lock_if_expired(db, article)
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
    _check_not_ai_locked(db, article)
    return to_article_read(update_article(db, article, payload))


# 删除文章
@router.delete("/{article_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_article_endpoint(
    article_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Response:
    article = _verify_article_ownership(get_article(db, article_id), current_user)
    _check_not_ai_locked(db, article)
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
        raise ConflictError("文章已被修改，请刷新后再保存")
    return to_article_read(set_article_cover(db, article, payload.cover_asset_id))


# 触发 AI 格式调整
@router.post("/{article_id}/ai-format", status_code=202)
def trigger_ai_format_endpoint(
    article_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    article = _verify_article_ownership(get_article(db, article_id), current_user)
    _check_not_ai_locked(db, article)
    from server.app.modules.articles.ai_format import has_ai_format_targets

    if not has_ai_format_targets(article.content_json):
        raise ClientError("文章正文为空，无法进行 AI 格式调整")

    lock_started_at = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    article.ai_checking = True
    article.ai_checking_started_at = lock_started_at
    article.ai_format_error = None
    db.commit()

    def _run() -> None:
        try:
            from server.app.modules.articles.ai_format import run_ai_format
            run_ai_format(article_id, include_images=False, lock_started_at=lock_started_at)
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "ai_format background thread crashed for article %s", article_id
            )
            try:
                from server.app.db.session import SessionLocal
                from server.app.modules.articles.ai_format import _describe_ai_format_error, _unlock_ai_format
                cleanup_db = SessionLocal()
                try:
                    _unlock_ai_format(
                        cleanup_db,
                        article_id,
                        lock_started_at,
                        error_message=_describe_ai_format_error(exc),
                    )
                finally:
                    cleanup_db.close()
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}

