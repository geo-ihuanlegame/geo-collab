"""
文章 / 文章分组 业务逻辑层（CRUD + 审核 + 全文检索）。

约定：
  - 软删除（is_deleted），查询一律过滤 is_deleted == False。
  - 乐观锁：update_* 用 payload.version 对比 article/group.version，不一致抛 ConflictError；每次写 version+1。
  - PATCH 语义：update_article 跳过值为 None 的字段（见 CLAUDE.md「ArticleUpdate 丢 null」），
    唯一例外是 stock_category_id 允许显式置 None 来解除关联。
  - 正文三份存储（content_json / content_html / plain_text）+ body_assets 由调用方/sync 同步。
  - review_status：pending=待审 / approved=已审，未过审不可发布。
"""

from __future__ import annotations

import logging

from sqlalchemy import bindparam, func, select, text
from sqlalchemy import delete as sa_delete
from sqlalchemy.orm import Session, selectinload

from server.app.core.time import utcnow
from server.app.modules.articles.models import (
    Article,
    ArticleBodyAsset,
    ArticleGroup,
    ArticleGroupItem,
    Asset,
)
from server.app.modules.articles.parser import (
    dumps_content_json,
    extract_body_image_nodes,
    loads_content_json,
)
from server.app.modules.articles.schemas import (
    ArticleCreate,
    ArticleGroupCreate,
    ArticleGroupItemsUpdate,
    ArticleGroupUpdate,
    ArticleUpdate,
)
from server.app.modules.tasks.models import PublishRecord, PublishTask
from server.app.shared.errors import ClientError, ConflictError

_logger = logging.getLogger(__name__)

VALID_ARTICLE_STATUSES = {"draft", "ready", "archived"}
VALID_REVIEW_STATUSES = {"pending", "approved"}


def validate_article_status(status: str) -> None:
    if status not in VALID_ARTICLE_STATUSES:
        raise ClientError(f"Invalid article status: {status}")


def ensure_asset_exists(db: Session, asset_id: str | None) -> None:
    if asset_id is None:
        return
    if db.get(Asset, asset_id) is None:
        raise ClientError(f"Asset not found: {asset_id}")


def sync_article_body_assets(db: Session, article: Article, content_json: dict) -> None:
    """按正文 JSON 里的图片节点重建 body_assets 关联（先全清再按文档顺序重建，position 即顺序）。"""
    image_nodes = extract_body_image_nodes(content_json)
    for asset_id, _ in image_nodes:
        ensure_asset_exists(db, asset_id)

    article.body_assets.clear()
    for position, (asset_id, editor_node_id) in enumerate(image_nodes):
        article.body_assets.append(
            ArticleBodyAsset(
                asset_id=asset_id,
                position=position,
                editor_node_id=editor_node_id,
            )
        )


def get_article(db: Session, article_id: int) -> Article | None:
    stmt = (
        select(Article)
        .where(Article.id == article_id, Article.is_deleted == False)  # noqa: E712
        .options(
            selectinload(Article.body_assets).selectinload(ArticleBodyAsset.asset),
            selectinload(Article.stock_categories),
        )
    )
    return db.execute(stmt).scalar_one_or_none()


def _search_articles(db: Session, query: str, user_id: int | None = None) -> list[Article]:
    # MySQL FULLTEXT（ngram parser）自然语言检索 title/author/plain_text。
    # 自然语言模式（不带 IN BOOLEAN MODE）：用户输入里的 + - " * ( ) 等被当词分隔符、不当布尔操作符，
    #   故无需转义、不会因特殊字符触发 syntax error 或返回诡异空集。query 走绑定参数 :q（防注入）；
    #   列名写死在 SQL 文本里（非用户可控）。无 FTS 索引（如某些环境漏建）时本句会抛，由调用方 except 回退 LIKE。
    # 注意：SQLAlchemy 的 func.match(...).against(...) 在 2.x 不可用（Function 无 .against），曾导致检索
    #   永远静默退化成 LIKE、ngram 索引空转（见 issue #50），故这里直接用 text() 显式构造。
    match_clause = text(
        "MATCH (articles.title, articles.author, articles.plain_text) AGAINST (:q) > 0"
    ).bindparams(bindparam("q", query))
    stmt = select(Article).where(
        Article.is_deleted == False,  # noqa: E712
        match_clause,
    )

    if user_id is not None:
        stmt = stmt.where(Article.user_id == user_id)

    return list(db.execute(stmt).scalars().all())


def list_articles(
    db: Session,
    query: str | None = None,
    skip: int = 0,
    limit: int = 50,
    user_id: int | None = None,
    review_status: str | None = None,
) -> list[Article]:
    # query ≥3 字才走 FTS（ngram 最短 token）；FTS 不可用时 except 落到下面的 LIKE 回退
    if query and len(query) >= 3:
        try:
            matching = _search_articles(db, query, user_id=user_id)
            if review_status is not None:
                matching = [a for a in matching if a.review_status == review_status]
            if not matching:
                return []
            matching.sort(key=lambda a: a.updated_at, reverse=True)
            ids = [a.id for a in matching[skip : skip + limit]]
            if not ids:
                return []
            stmt = (
                select(Article)
                .options(selectinload(Article.body_assets))
                .where(Article.id.in_(ids), Article.is_deleted == False)  # noqa: E712
                .order_by(Article.updated_at.desc())
            )
            articles = list(db.execute(stmt).scalars().all())
            articles.sort(key=lambda a: ids.index(a.id))
            return articles
        except Exception:
            _logger.debug("FTS search unavailable, falling back to LIKE query", exc_info=True)

    stmt = (
        select(Article)
        .where(Article.is_deleted == False)  # noqa: E712
        .options(selectinload(Article.body_assets))
        .order_by(Article.updated_at.desc())
    )

    if user_id is not None:
        stmt = stmt.where(Article.user_id == user_id)

    if review_status is not None:
        stmt = stmt.where(Article.review_status == review_status)

    if query:
        like = f"%{query}%"
        stmt = stmt.where(
            (Article.title.like(like))
            | (Article.author.like(like))
            | (Article.plain_text.like(like))
        )

    stmt = stmt.offset(skip).limit(limit)
    return list(db.execute(stmt).scalars().all())


def create_article(db: Session, user_id: int, payload: ArticleCreate) -> Article:
    """新建文章。client_request_id 做幂等：已存在同 request_id 的文章直接返回，不重复创建。"""
    # 软幂等：先按 client_request_id 全局预查（不限 user）；并发下由 per-user 唯一约束 uq_articles_user_client_request_id 兜底（router 捕 IntegrityError 再按 user 查一次）
    if payload.client_request_id:
        existing = db.execute(
            select(Article).where(
                Article.client_request_id == payload.client_request_id,
                Article.is_deleted == False,  # noqa: E712
            )
        ).scalar_one_or_none()
        if existing is not None:
            return get_article(db, existing.id) or existing

    validate_article_status(payload.status)
    ensure_asset_exists(db, payload.cover_asset_id)
    article = Article(
        user_id=user_id,
        title=payload.title,
        author=payload.author,
        cover_asset_id=payload.cover_asset_id,
        content_json=dumps_content_json(payload.content_json),
        content_html=payload.content_html,
        plain_text=payload.plain_text,
        word_count=payload.word_count,
        status=payload.status,
        client_request_id=payload.client_request_id,
    )
    sync_article_body_assets(db, article, payload.content_json)
    db.add(article)
    db.flush()
    return get_article(db, article.id) or article


def update_article(db: Session, article: Article, payload: ArticleUpdate) -> Article:
    """局部更新文章（乐观锁 + None 跳过语义）。改 content_json 时同步 body_assets 并重算 version。"""
    update_data = payload.model_dump(exclude_unset=True)
    expected_version = update_data.pop("version", None)
    if expected_version is not None and article.version != expected_version:
        raise ConflictError("Article has been modified; refresh before saving")

    if "status" in update_data and update_data["status"] is not None:
        validate_article_status(update_data["status"])
    if "cover_asset_id" in update_data:
        ensure_asset_exists(db, update_data["cover_asset_id"])

    content_json = loads_content_json(article.content_json)
    if "content_json" in update_data and update_data["content_json"] is not None:
        content_json = update_data["content_json"]

    # 显式过滤 None：PATCH {"field": null} 不会清空字段（见 CLAUDE.md「ArticleUpdate 丢 null」）
    for field in (
        "title",
        "author",
        "cover_asset_id",
        "content_html",
        "plain_text",
        "word_count",
        "status",
    ):
        if field in update_data and update_data[field] is not None:
            setattr(article, field, update_data[field])
    # stock_category_id 允许显式置 None（移除关联）
    if "stock_category_id" in update_data:
        article.stock_category_id = update_data["stock_category_id"]

    # 多对多栏目：如果传了 stock_category_ids，更新关联表
    if "stock_category_ids" in update_data:
        from server.app.modules.image_library.models import StockCategory as _StockCategory

        cat_ids = update_data["stock_category_ids"] or []
        if cat_ids:
            cats = list(
                db.execute(select(_StockCategory).where(_StockCategory.id.in_(cat_ids)))
                .scalars()
                .all()
            )
        else:
            cats = []
        article.stock_categories = cats
    elif "stock_category_id" in update_data and update_data["stock_category_id"] is not None:
        # 兼容旧字段：如果只传了 stock_category_id 且多对多列表为空，把旧值塞进多对多
        from server.app.modules.image_library.models import StockCategory as _StockCategory

        if not article.stock_categories:
            cat = db.get(_StockCategory, update_data["stock_category_id"])
            if cat is not None:
                article.stock_categories = [cat]

    if "content_json" in update_data:
        article.content_json = dumps_content_json(content_json)
        sync_article_body_assets(db, article, content_json)

    article.version += 1
    article.updated_at = utcnow()
    db.flush()
    return get_article(db, article.id) or article


def set_article_cover(db: Session, article: Article, cover_asset_id: str | None) -> Article:
    ensure_asset_exists(db, cover_asset_id)
    article.cover_asset_id = cover_asset_id
    article.version += 1
    article.updated_at = utcnow()
    db.flush()
    return get_article(db, article.id) or article


def delete_article(db: Session, article: Article) -> None:
    """软删除文章。存在未完成发布记录则拒删；删前清掉其所有分组关联（硬删 ArticleGroupItem）。"""
    article_id = article.id

    active = (
        db.execute(
            select(PublishRecord.id).where(
                PublishRecord.article_id == article_id,
                PublishRecord.status.in_(
                    ["pending", "running", "waiting_manual_publish", "waiting_user_input"]
                ),
            )
        )
        .scalars()
        .all()
    )
    if active:
        raise ClientError("存在未完成发布记录，无法删除文章")

    db.execute(sa_delete(ArticleGroupItem).where(ArticleGroupItem.article_id == article_id))
    article.is_deleted = True
    article.deleted_at = utcnow()
    article.updated_at = utcnow()
    db.flush()


# --- Article review (审核) ---


def _get_owned_article(db: Session, article_id: int, user_id: int, role: str) -> Article:
    """按所有权取文章；非 admin 只能取自己的。找不到 / 越权 → ClientError(404 语义)。"""
    article = get_article(db, article_id)
    if article is None or (role != "admin" and article.user_id != user_id):
        raise ClientError("文章不存在")
    return article


def _set_article_review_status(article: Article, review_status: str) -> Article:
    article.review_status = review_status
    article.version += 1
    article.updated_at = utcnow()
    return article


def approve_article(db: Session, article_id: int, user_id: int, role: str) -> Article:
    """通过审核：置 review_status='approved'，version+1。"""
    article = _get_owned_article(db, article_id, user_id, role)
    _set_article_review_status(article, "approved")
    db.flush()
    return get_article(db, article.id) or article


def revoke_article_approval(db: Session, article_id: int, user_id: int, role: str) -> Article:
    """撤销审核：打回 review_status='pending'，version+1。"""
    article = _get_owned_article(db, article_id, user_id, role)
    _set_article_review_status(article, "pending")
    db.flush()
    return get_article(db, article.id) or article


# --- Article Group CRUD ---


def get_group(db: Session, group_id: int) -> ArticleGroup | None:
    stmt = (
        select(ArticleGroup)
        .where(ArticleGroup.id == group_id, ArticleGroup.is_deleted == False)  # noqa: E712
        .options(selectinload(ArticleGroup.items).selectinload(ArticleGroupItem.article))
    )
    return db.execute(stmt).scalar_one_or_none()


def list_groups(db: Session) -> list[ArticleGroup]:
    stmt = (
        select(ArticleGroup)
        .where(ArticleGroup.is_deleted == False)  # noqa: E712
        .options(selectinload(ArticleGroup.items))
        .order_by(ArticleGroup.updated_at.desc())
    )
    return list(db.execute(stmt).scalars().all())


def create_group(db: Session, user_id: int, payload: ArticleGroupCreate) -> ArticleGroup:
    """新建分组。撞到同名软删分组则原地复活（清空成员、刷新元数据），绕开 (user_id, name) 唯一约束。"""
    existing = db.execute(
        select(ArticleGroup).where(
            ArticleGroup.user_id == user_id, ArticleGroup.name == payload.name
        )
    ).scalar_one_or_none()
    # 同名分组已软删 → 复活而非新建（否则唯一约束会冲突）
    if existing is not None and existing.is_deleted:
        existing.description = payload.description
        existing.is_deleted = False
        existing.deleted_at = None
        existing.version += 1
        existing.updated_at = utcnow()
        existing.items.clear()
        db.flush()
        return get_group(db, existing.id) or existing

    group = ArticleGroup(user_id=user_id, name=payload.name, description=payload.description)
    db.add(group)
    db.flush()
    return get_group(db, group.id) or group


def update_group(db: Session, group: ArticleGroup, payload: ArticleGroupUpdate) -> ArticleGroup:
    update_data = payload.model_dump(exclude_unset=True)
    expected_version = update_data.pop("version", None)
    if expected_version is not None and group.version != expected_version:
        raise ConflictError("Article group has been modified; refresh before saving")

    for field in ("name", "description"):
        if field in update_data:
            setattr(group, field, update_data[field])
    group.version += 1
    group.updated_at = utcnow()
    db.flush()
    return get_group(db, group.id) or group


def replace_group_items(
    db: Session, group: ArticleGroup, payload: ArticleGroupItemsUpdate
) -> ArticleGroup:
    """整组替换成员（先校验去重 + 文章存在，再清空重建）。乐观锁，未传 sort_order 用下标兜底。"""
    if payload.version is not None and group.version != payload.version:
        raise ConflictError("Article group has been modified; refresh before saving")

    seen: set[int] = set()
    article_ids: list[int] = []
    for item in payload.items:
        if item.article_id in seen:
            raise ClientError(f"Duplicate article_id: {item.article_id}")
        seen.add(item.article_id)
        article_ids.append(item.article_id)

    if article_ids:
        existing_ids = set(
            db.execute(
                select(Article.id).where(
                    Article.id.in_(article_ids),
                    Article.is_deleted == False,  # noqa: E712
                )
            )
            .scalars()
            .all()
        )
        missing_ids = [aid for aid in article_ids if aid not in existing_ids]
        if missing_ids:
            raise ClientError(f"Article not found: {missing_ids[0]}")

    group.items.clear()
    db.flush()
    for index, item in enumerate(payload.items):
        group.items.append(
            ArticleGroupItem(
                article_id=item.article_id,
                sort_order=item.sort_order if item.sort_order is not None else index,
            )
        )
    group.updated_at = utcnow()
    group.version += 1
    db.flush()
    return get_group(db, group.id) or group


def delete_group(db: Session, group: ArticleGroup) -> None:
    """软删除分组。存在 pending/running 的发布任务则拒删。"""
    active_task = db.execute(
        select(PublishTask.id).where(
            PublishTask.group_id == group.id,
            PublishTask.status.in_(["pending", "running"]),
        )
    ).scalar_one_or_none()
    if active_task:
        raise ClientError("存在未完成发布任务，无法删除分组")

    group.is_deleted = True
    group.deleted_at = utcnow()
    group.updated_at = utcnow()
    db.flush()


# --- Article group review (整组审核) ---


def compute_group_review_summary(db: Session, group_id: int) -> tuple[int, int]:
    """返回 (total, approved)：组内未删除文章总数 / 已审核数。

    「整组已审核」由调用方判断：approved == total and total > 0。
    """
    base = (
        select(ArticleGroupItem.article_id)
        .join(Article, Article.id == ArticleGroupItem.article_id)
        .where(
            ArticleGroupItem.group_id == group_id,
            Article.is_deleted == False,  # noqa: E712
        )
    )
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    approved = db.execute(
        select(func.count()).select_from(base.where(Article.review_status == "approved").subquery())
    ).scalar_one()
    return int(total), int(approved)


def approve_group(db: Session, group_id: int, user_id: int, role: str) -> ArticleGroup:
    """把组内所有未删除文章置 review_status='approved'（version+1）。"""
    group = get_group(db, group_id)
    if group is None or (role != "admin" and group.user_id != user_id):
        raise ClientError("文章分组不存在")

    article_ids = [item.article_id for item in group.items]
    if article_ids:
        articles = list(
            db.execute(
                select(Article).where(
                    Article.id.in_(article_ids),
                    Article.is_deleted == False,  # noqa: E712
                    Article.review_status != "approved",
                )
            )
            .scalars()
            .all()
        )
        for article in articles:
            _set_article_review_status(article, "approved")
        db.flush()
    return get_group(db, group_id) or group


def mark_pending_and_group(
    session_factory,
    *,
    article_ids: list[int],
    user_id: int,
    base_name: str,
    fallback_suffix: str | None = None,
) -> int | None:
    """把文章标 review_status='pending' 并归入一个新 ArticleGroup（名 base_name）。
    撞 (user_id, name) 唯一约束时改用 base_name + fallback_suffix（调用方应传稳定唯一值，
    如 run_id；未传则回退到不稳定的 #article_ids[0] 旧行为）。best-effort：失败记日志、不抛。
    用独立 session、本函数内 commit+close。返回 group_id 或 None。"""
    if not article_ids:
        return None
    suffix = fallback_suffix or f"#{article_ids[0]}"
    try:
        from sqlalchemy.exc import IntegrityError

        db = session_factory()
        try:
            for aid in article_ids:
                art = db.get(Article, aid)
                if art is not None:
                    art.review_status = "pending"

            exists = (
                db.query(ArticleGroup.id)
                .filter(
                    ArticleGroup.user_id == user_id,
                    ArticleGroup.name == base_name,
                    ArticleGroup.is_deleted.is_(False),
                )
                .first()
            )
            name = f"{base_name} {suffix}" if exists is not None else base_name
            group = ArticleGroup(user_id=user_id, name=name)
            db.add(group)
            try:
                db.flush()
            except IntegrityError:
                # 并发抢到了 base_name（唯一约束冲突）：rollback 丢掉本次未提交改动，
                # 重新标 pending 并改用带 suffix 的名字重试一次
                db.rollback()
                for aid in article_ids:
                    art = db.get(Article, aid)
                    if art is not None:
                        art.review_status = "pending"
                group = ArticleGroup(user_id=user_id, name=f"{base_name} {suffix}")
                db.add(group)
                db.flush()

            for idx, aid in enumerate(article_ids):
                db.add(ArticleGroupItem(group_id=group.id, article_id=aid, sort_order=idx))
            gid = group.id
            db.commit()
            return gid
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — best-effort
        _logger.exception(
            "mark_pending_and_group failed (user=%s, n=%s)", user_id, len(article_ids)
        )
        return None
