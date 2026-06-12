"""文章模块路由。"""

import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.app.core.config import get_settings
from server.app.core.paths import get_data_dir
from server.app.core.security import get_current_user, require_admin
from server.app.db.session import get_db
from server.app.modules.articles import (
    VALID_REVIEW_STATUSES,
    approve_article,
    approve_group,
    compute_group_review_summary,
    create_article,
    create_group,
    delete_article,
    delete_group,
    get_article,
    get_group,
    list_articles,
    list_groups,
    replace_group_items,
    revoke_article_approval,
    set_article_cover,
    update_article,
    update_group,
)
from server.app.modules.articles.models import Article, ArticleGroup, Asset
from server.app.modules.articles.schemas import (
    ArticleCoverUpdate,
    ArticleCreate,
    ArticleGroupCreate,
    ArticleGroupItemsUpdate,
    ArticleGroupRead,
    ArticleGroupUpdate,
    ArticleListRead,
    ArticleRead,
    ArticleUpdate,
    AssetRead,
    ReviewSummary,
    to_article_read,
    to_group_read,
)
from server.app.modules.articles.store import (
    _create_asset_from_path,
    asset_url,
    find_orphan_asset_ids,
    get_asset_stats,
    guess_image_size,
    normalize_ext,
    resolve_asset_path,
    soft_delete_assets,
    store_upload,
)
from server.app.modules.articles.uploader import (
    CHUNK_SIZE,
    MAGIC_BYTES_CHECK_SIZE,
    get_upload_manager,
)
from server.app.modules.audit.service import add_audit_entry
from server.app.modules.system.models import User
from server.app.modules.tasks.models import PublishRecord
from server.app.shared.errors import ClientError, ConflictError

articles_router = APIRouter()
article_groups_router = APIRouter()
assets_router = APIRouter()
chunked_assets_router = APIRouter()

_logger = logging.getLogger(__name__)


class AIFormatRequest(BaseModel):
    preset_id: int | None = None


# ── 文章辅助函数 ────────────────────────────────────────────────────────────


def _verify_article_ownership(article: Article | None, current_user: User) -> Article:
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    if current_user.role != "admin" and article.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="文章不存在")
    return article


def _is_ai_lock_expired(article: Article) -> bool:
    # 排版锁是否超时：started 为空（异常状态）视为过期；否则超过 ai_format_timeout_seconds 即过期
    if not article.ai_checking:
        return False
    started = article.ai_checking_started_at
    if started is None:
        return True
    elapsed = (datetime.now(UTC).replace(tzinfo=None) - started).total_seconds()
    return elapsed >= get_settings().ai_format_timeout_seconds


def _clear_ai_lock_if_expired(db: Session, article: Article) -> None:
    # 惰性解锁：读/改文章时顺手清掉超时未释放的排版锁（后台线程崩了也不会让文章永久卡 ai_checking）
    if not _is_ai_lock_expired(article):
        return
    article.ai_checking = False
    article.ai_checking_started_at = None
    article.ai_format_error = "AI 排版超时：模型服务响应超时或后台任务未完成，请重试。"
    db.commit()
    db.refresh(article)


def _check_not_ai_locked(db: Session, article: Article) -> None:
    """文章正在进行 AI 排版时抛 ConflictError。"""
    _clear_ai_lock_if_expired(db, article)
    if not article.ai_checking:
        return
    raise ConflictError("文章正在进行 AI 格式调整，请稍后再试")


# ── 文章路由 ────────────────────────────────────────────────────────────────


@articles_router.get("", response_model=list[ArticleListRead])
def read_articles(
    q: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    review_status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ArticleListRead]:
    if review_status is not None and review_status not in VALID_REVIEW_STATUSES:
        raise ClientError(f"Invalid review_status: {review_status}")
    articles = list_articles(
        db,
        q,
        skip=skip,
        limit=limit,
        user_id=None if current_user.role == "admin" else current_user.id,
        review_status=review_status,
    )
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
            review_status=a.review_status,
            published_count=count_map.get(a.id, 0),
            created_at=a.created_at,
            updated_at=a.updated_at,
        )
        for a in articles
    ]


@articles_router.post("", response_model=ArticleRead)
def create_article_endpoint(
    payload: ArticleCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleRead:
    try:
        created = create_article(db, current_user.id, payload)
        add_audit_entry(
            db,
            user=current_user,
            action="article.create",
            target_type="article",
            target_id=created.id,
            payload={"title": created.title},
            request=request,
        )
        return to_article_read(created)
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
                # 幂等重试：并发请求已经创建了这篇文章。
                return to_article_read(get_article(db, existing.id) or existing)
        # 上面的幂等查询无法消解的 IntegrityError 都是真实约束冲突；
        # 明确抛 409，避免隐式 return None 被序列化成不透明的 500。
        raise HTTPException(
            status_code=409, detail="请求冲突：client_request_id 已存在或数据完整性约束失败"
        ) from exc


@articles_router.get("/{article_id}", response_model=ArticleRead)
def read_article(
    article_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleRead:
    article = _verify_article_ownership(get_article(db, article_id), current_user)
    _clear_ai_lock_if_expired(db, article)
    return to_article_read(article)


@articles_router.put("/{article_id}", response_model=ArticleRead)
def update_article_endpoint(
    article_id: int,
    payload: ArticleUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleRead:
    article = _verify_article_ownership(get_article(db, article_id), current_user)
    _check_not_ai_locked(db, article)
    changed_fields = sorted(payload.model_dump(exclude_unset=True).keys())
    updated = update_article(db, article, payload)
    add_audit_entry(
        db,
        user=current_user,
        action="article.update",
        target_type="article",
        target_id=article_id,
        payload={"changed_fields": changed_fields},
        request=request,
    )
    return to_article_read(updated)


@articles_router.delete("/{article_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_article_endpoint(
    article_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Response:
    article = _verify_article_ownership(get_article(db, article_id), current_user)
    _check_not_ai_locked(db, article)
    article_title = article.title
    delete_article(db, article)
    add_audit_entry(
        db,
        user=current_user,
        action="article.delete",
        target_type="article",
        target_id=article_id,
        payload={"title": article_title},
        request=request,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@articles_router.post("/{article_id}/cover", response_model=ArticleRead)
def update_article_cover(
    article_id: int,
    payload: ArticleCoverUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleRead:
    article = _verify_article_ownership(get_article(db, article_id), current_user)
    if payload.version is not None and article.version != payload.version:
        raise ConflictError("文章已被修改，请刷新后再保存")
    updated = set_article_cover(db, article, payload.cover_asset_id)
    add_audit_entry(
        db,
        user=current_user,
        action="article.cover.update",
        target_type="article",
        target_id=article_id,
        payload={"asset_id": payload.cover_asset_id},
        request=request,
    )
    return to_article_read(updated)


@articles_router.post("/{article_id}/approve", response_model=ArticleRead)
def approve_article_endpoint(
    article_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleRead:
    article = _verify_article_ownership(get_article(db, article_id), current_user)
    updated = approve_article(db, article.id, current_user.id, current_user.role)
    add_audit_entry(
        db,
        user=current_user,
        action="article.review.approve",
        target_type="article",
        target_id=article_id,
        payload=None,
        request=request,
    )
    return to_article_read(updated)


@articles_router.post("/{article_id}/revoke-approval", response_model=ArticleRead)
def revoke_article_approval_endpoint(
    article_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleRead:
    article = _verify_article_ownership(get_article(db, article_id), current_user)
    updated = revoke_article_approval(db, article.id, current_user.id, current_user.role)
    add_audit_entry(
        db,
        user=current_user,
        action="article.review.revoke",
        target_type="article",
        target_id=article_id,
        payload=None,
        request=request,
    )
    return to_article_read(updated)


@articles_router.post("/{article_id}/ai-format", status_code=202)
def trigger_ai_format_endpoint(
    article_id: int,
    request: Request,
    payload: AIFormatRequest | None = Body(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """触发 AI 排版：抢锁（置 ai_checking）后立即启动后台线程跑 run_ai_format，202 返回。

    is_checking 期间不可重复触发；含配图栏目时附带自动配图。线程崩溃由 _run 的 except 兜底解锁。
    """
    article = _verify_article_ownership(get_article(db, article_id), current_user)
    _check_not_ai_locked(db, article)
    from server.app.modules.articles.ai_format import has_ai_format_targets

    if not has_ai_format_targets(article.content_json):
        raise ClientError("文章正文为空，无法进行 AI 格式调整")

    preset_id = (
        payload.preset_id
        if payload and payload.preset_id is not None
        else current_user.ai_format_preset_id
    )
    if preset_id is not None:
        from server.app.modules.prompt_templates.service import get_visible_prompt_template

        preset = get_visible_prompt_template(
            db, preset_id, user_id=current_user.id, scope="ai_format"
        )
        if preset is None or not preset.is_enabled:
            raise HTTPException(status_code=404, detail="AI format prompt preset not found")

    # lock_started_at 当锁指纹传给后台线程，run_ai_format 写回前据此判断锁是否仍属本次
    lock_started_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    include_images = (
        article.stock_category_id is not None or len(article.stock_categories or []) > 0
    )
    # 抢锁并先 commit：让后台线程和后续请求都能立刻看到 ai_checking=True
    article.ai_checking = True
    article.ai_checking_started_at = lock_started_at
    article.ai_format_error = None
    db.commit()

    def _run() -> None:
        try:
            from server.app.modules.articles.ai_format import run_ai_format

            run_ai_format(
                article_id,
                include_images=include_images,
                lock_started_at=lock_started_at,
                preset_id=preset_id,
                user_id=current_user.id,
            )
        except Exception as exc:
            # 线程崩溃兜底：run_ai_format 内部异常已自解锁，但若它在解锁前就崩了，
            # 这里用独立 session 强制解锁，绝不让文章永久卡在 ai_checking=True
            logging.getLogger(__name__).exception(
                "ai_format background thread crashed for article %s", article_id
            )
            try:
                from server.app.db.session import SessionLocal
                from server.app.modules.articles.ai_format import (
                    _describe_ai_format_error,
                    _unlock_ai_format,
                )

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
    add_audit_entry(
        db,
        user=current_user,
        action="article.ai_format.trigger",
        target_type="article",
        target_id=article_id,
        payload=None,
        request=request,
    )
    return {"status": "started"}


# ── 文章分组辅助函数 ────────────────────────────────────────────────────────


def _verify_group_ownership(group: ArticleGroup | None, current_user: User) -> ArticleGroup:
    if group is None:
        raise HTTPException(status_code=404, detail="文章分组不存在")
    if current_user.role != "admin" and group.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="文章分组不存在")
    return group


def _group_read_with_summary(db: Session, group: ArticleGroup) -> ArticleGroupRead:
    total, approved = compute_group_review_summary(db, group.id)
    return to_group_read(group, ReviewSummary(total=total, approved=approved))


# ── 文章分组路由 ────────────────────────────────────────────────────────────


@article_groups_router.get("", response_model=list[ArticleGroupRead])
def read_groups(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ArticleGroupRead]:
    groups = list_groups(db)
    if current_user.role != "admin":
        groups = [g for g in groups if g.user_id == current_user.id]
    return [_group_read_with_summary(db, group) for group in groups]


@article_groups_router.post("", response_model=ArticleGroupRead)
def create_group_endpoint(
    payload: ArticleGroupCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleGroupRead:
    try:
        group = create_group(db, current_user.id, payload)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="分组名称已存在") from exc
    add_audit_entry(
        db,
        user=current_user,
        action="article_group.create",
        target_type="article_group",
        target_id=group.id,
        payload={"name": group.name},
        request=request,
    )
    return _group_read_with_summary(db, group)


@article_groups_router.get("/{group_id}", response_model=ArticleGroupRead)
def read_group(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleGroupRead:
    group = _verify_group_ownership(get_group(db, group_id), current_user)
    return _group_read_with_summary(db, group)


@article_groups_router.put("/{group_id}", response_model=ArticleGroupRead)
def update_group_endpoint(
    group_id: int,
    payload: ArticleGroupUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleGroupRead:
    group = _verify_group_ownership(get_group(db, group_id), current_user)
    changed_fields = sorted(payload.model_dump(exclude_unset=True).keys())
    try:
        updated = update_group(db, group, payload)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="分组名称已存在") from exc
    add_audit_entry(
        db,
        user=current_user,
        action="article_group.update",
        target_type="article_group",
        target_id=group_id,
        payload={"changed_fields": changed_fields},
        request=request,
    )
    return _group_read_with_summary(db, updated)


@article_groups_router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group_endpoint(
    group_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Response:
    group = _verify_group_ownership(get_group(db, group_id), current_user)
    group_name = group.name
    try:
        delete_group(db, group)
    except ClientError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    add_audit_entry(
        db,
        user=current_user,
        action="article_group.delete",
        target_type="article_group",
        target_id=group_id,
        payload={"name": group_name},
        request=request,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@article_groups_router.put("/{group_id}/items", response_model=ArticleGroupRead)
def update_group_items(
    group_id: int,
    payload: ArticleGroupItemsUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleGroupRead:
    group = _verify_group_ownership(get_group(db, group_id), current_user)
    updated = replace_group_items(db, group, payload)
    add_audit_entry(
        db,
        user=current_user,
        action="article_group.items.replace",
        target_type="article_group",
        target_id=group_id,
        payload={"item_count": len(payload.items)},
        request=request,
    )
    return _group_read_with_summary(db, updated)


@article_groups_router.post("/{group_id}/approve-all", response_model=ArticleGroupRead)
def approve_group_endpoint(
    group_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleGroupRead:
    group = _verify_group_ownership(get_group(db, group_id), current_user)
    updated = approve_group(db, group.id, current_user.id, current_user.role)
    add_audit_entry(
        db,
        user=current_user,
        action="article_group.review.approve_all",
        target_type="article_group",
        target_id=group_id,
        payload=None,
        request=request,
    )
    return _group_read_with_summary(db, updated)


# ── 资产辅助函数 ────────────────────────────────────────────────────────────


def resolve_asset_path_from_storage_key(storage_key: str) -> Path | None:
    """根据 storage_key 解析磁盘路径；路径逃逸时返回 None。"""
    try:
        data_dir = get_data_dir().resolve()
        path = (data_dir / storage_key).resolve()
        if data_dir != path and data_dir not in path.parents:
            return None
        return path
    except Exception:
        return None


def to_asset_read(asset: Asset) -> AssetRead:
    return AssetRead(
        id=asset.id,
        filename=asset.filename,
        ext=asset.ext,
        mime_type=asset.mime_type,
        size=asset.size,
        sha256=asset.sha256,
        storage_key=asset.storage_key,
        width=asset.width,
        height=asset.height,
        created_at=asset.created_at,
        url=asset_url(asset.id),
    )


# ── 资产路由 ────────────────────────────────────────────────────────────────


@assets_router.post("", response_model=AssetRead)
async def upload_asset(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    stored = await store_upload(db, current_user.id, file)
    return Response(
        content=to_asset_read(stored.asset).model_dump_json(),
        media_type="application/json",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@assets_router.get("/stats")
def asset_stats(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    """磁盘资产统计（总量、孤儿数、已删除数、缩略图缓存大小）。"""
    return get_asset_stats(db)


@assets_router.post("/cleanup-orphans")
def cleanup_orphan_assets(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> dict:
    """将所有孤儿资产（未被任何文章引用）标记为逻辑删除。不删除磁盘文件。"""
    orphan_ids = find_orphan_asset_ids(db)
    marked = soft_delete_assets(db, orphan_ids)
    add_audit_entry(
        db,
        user=current_user,
        action="asset.cleanup_orphans",
        target_type="asset",
        target_id=None,
        payload={"deleted_count": marked},
        request=request,
    )
    return {"orphan_count": len(orphan_ids), "marked_deleted": marked}


@assets_router.get("/{asset_id}/meta", response_model=AssetRead)
def read_asset_meta(asset_id: str, db: Session = Depends(get_db)) -> AssetRead:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资源不存在")
    return to_asset_read(asset)


@assets_router.get("/{asset_id}/thumbnail")
async def read_asset_thumbnail(
    asset_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """获取资产缩略图，如果缩略图不存在则 302 重定向到原图"""
    asset = db.get(Asset, asset_id)
    if asset is None or asset.is_deleted:
        raise HTTPException(status_code=404)

    # 优先返回缩略图
    if asset.thumb_storage_key:
        thumb_path = resolve_asset_path_from_storage_key(asset.thumb_storage_key)
        if thumb_path and thumb_path.exists():
            if os.environ.get("GEO_NGINX_ACCEL"):
                rel = thumb_path.relative_to(get_data_dir())
                return Response(
                    status_code=200,
                    headers={
                        "X-Accel-Redirect": f"/internal_data/{rel}",
                        "Content-Type": "image/webp",
                        "Cache-Control": "public, max-age=31536000, immutable",
                    },
                )
            return FileResponse(str(thumb_path), media_type="image/webp")

    # 回退：缩略图不存在则 302 重定向到原图
    return RedirectResponse(url=f"/api/assets/{asset_id}", status_code=302)


@assets_router.get("/{asset_id}")
def read_asset_file(
    asset_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """返回资产原图。Accept 带 image/webp 且有 webp 派生时改发 webp；GEO_NGINX_ACCEL 下走 X-Accel 卸载给 nginx。"""
    asset = db.get(Asset, asset_id)
    if asset is None or asset.is_deleted:
        raise HTTPException(status_code=404, detail="资源不存在")

    try:
        path = resolve_asset_path(asset)
    except (ClientError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not path.exists():
        raise HTTPException(status_code=404, detail="资源文件不存在")

    # WebP 内容协商
    accept = request.headers.get("accept", "")
    mime_type = asset.mime_type
    if "image/webp" in accept and asset.webp_storage_key:
        webp_path = resolve_asset_path_from_storage_key(asset.webp_storage_key)
        if webp_path and webp_path.exists():
            path = webp_path
            mime_type = "image/webp"

    if os.environ.get("GEO_NGINX_ACCEL"):
        rel = path.relative_to(get_data_dir())
        filename_rfc5987 = quote(asset.filename.encode("utf-8"), safe="")
        return Response(
            status_code=200,
            headers={
                "X-Accel-Redirect": f"/internal_data/{rel}",
                "Content-Type": mime_type,
                "Content-Disposition": f"inline; filename*=UTF-8''{filename_rfc5987}",
                "Cache-Control": "public, max-age=31536000, immutable",
            },
        )

    filename_rfc5987 = quote(asset.filename.encode("utf-8"), safe="")
    return FileResponse(
        path,
        media_type=mime_type,
        filename=filename_rfc5987,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# ── 分块上传请求模型 ────────────────────────────────────────────────────────


class ChunkedUploadStartRequest(BaseModel):
    total_size: int
    file_hash: str | None = None  # 已弃用：仅为旧客户端保留。


class ChunkedUploadCompleteRequest(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"


# ── 分块资产路由 ────────────────────────────────────────────────────────────


@chunked_assets_router.post("/upload-start")
async def start_chunked_upload(
    payload: ChunkedUploadStartRequest | None = Body(default=None),
    total_size: int | None = Query(default=None),
    file_hash: str | None = Query(default=None),  # noqa: ARG001
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """初始化分块上传。"""
    from server.app.core.config import MAX_ASSET_BYTES

    if payload is not None:
        total_size = payload.total_size
    if total_size is None:
        raise HTTPException(status_code=422, detail="请提供文件大小")
    if total_size <= 0:
        raise HTTPException(status_code=400, detail="文件不能为空")
    if total_size > MAX_ASSET_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {MAX_ASSET_BYTES // (1024 * 1024)}MB 限制",
        )

    manager = get_upload_manager()
    session = manager.init_session(total_size)

    return {
        "upload_id": session.upload_id,
        "chunk_size": CHUNK_SIZE,
        "chunk_count": session.chunk_count,
    }


@chunked_assets_router.post("/upload-chunk/{upload_id}")
async def upload_chunk(
    upload_id: str,
    chunk_index: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """上传单个分块。"""
    manager = get_upload_manager()
    session = manager.get_session(upload_id)

    if not session:
        raise HTTPException(status_code=404, detail="上传会话不存在")

    if chunk_index < 0 or chunk_index >= session.chunk_count:
        raise HTTPException(status_code=400, detail="无效的分块索引")

    chunk_data = await file.read()

    if chunk_index < session.chunk_count - 1:
        if len(chunk_data) != CHUNK_SIZE:
            raise HTTPException(status_code=400, detail="分块大小不正确")
    else:
        expected_last_size = session.total_size - (session.chunk_count - 1) * CHUNK_SIZE
        if len(chunk_data) != expected_last_size:
            raise HTTPException(status_code=400, detail="最后一个分块大小不正确")

    await manager.save_chunk(upload_id, chunk_index, chunk_data)

    return {"status": "ok"}


@chunked_assets_router.post("/upload-status/{upload_id}")
async def get_upload_status(
    upload_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """获取上传进度。"""
    manager = get_upload_manager()
    session = manager.get_session(upload_id)

    if not session:
        raise HTTPException(status_code=404, detail="上传会话不存在")

    uploaded = manager.get_uploaded_chunks(upload_id)

    return {
        "chunk_count": session.chunk_count,
        "uploaded_chunks": sorted(list(uploaded)),
        "is_complete": manager.is_complete(upload_id),
    }


@chunked_assets_router.post("/upload-complete/{upload_id}")
async def complete_chunked_upload(
    upload_id: str,
    payload: ChunkedUploadCompleteRequest | None = Body(default=None),
    filename: str | None = Query(default=None),
    content_type: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """完成分块上传，合并所有分块并创建资源。"""
    if payload is not None:
        filename = payload.filename
        content_type = payload.content_type
    if filename is None:
        raise HTTPException(status_code=422, detail="请提供文件名")
    content_type = content_type or "application/octet-stream"

    manager = get_upload_manager()
    session = manager.get_session(upload_id)

    if not session:
        raise HTTPException(status_code=404, detail="上传会话不存在")

    if not manager.is_complete(upload_id):
        raise HTTPException(status_code=400, detail="文件尚未上传完毕")

    try:
        import asyncio

        loop = asyncio.get_event_loop()
        merged_path, sha256_hash, is_valid_format, format_error = await loop.run_in_executor(
            None, manager.merge_chunks, upload_id
        )

        if not is_valid_format:
            merged_path.unlink()
            raise HTTPException(status_code=415, detail=format_error or "Unsupported file type")

        file_header = merged_path.read_bytes()[:MAGIC_BYTES_CHECK_SIZE]

        ext = normalize_ext(filename, content_type, file_header)
        width, height = guess_image_size(file_header)

        stored = await loop.run_in_executor(
            None,
            _create_asset_from_path,
            db,
            current_user.id,
            merged_path,
            filename,
            content_type,
            sha256_hash,
            session.total_size,
            ext,
            width,
            height,
            True,
        )

        return to_asset_read(stored.asset).model_dump()

    except HTTPException:
        # 必须重新抛出（如 415），不要包成 500（见 CLAUDE.md「complete_chunked_upload」约束）
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        try:
            manager.cleanup_session(upload_id)
        except Exception:
            _logger.warning("Failed to cleanup chunked upload session %s", upload_id, exc_info=True)
