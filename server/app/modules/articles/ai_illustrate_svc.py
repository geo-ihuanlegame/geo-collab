"""ai_illustrate_svc —— 单篇文章「AI 智能配图 + 自动封面」的共享 service.

被 pipelines/nodes/ai_illustrate.py 和 articles MCP endpoint 共用，
保证两条路径配图效果完全一致.

不并发（单文章），调用方按需自管 ThreadPoolExecutor 包多篇.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from server.app.core.time import utcnow
from server.app.modules.articles.ai_format import (
    category_contexts_for,
    has_ai_format_targets,
    run_ai_format,
)
from server.app.modules.articles.models import Article
from server.app.modules.image_library.cover import (
    CoverResult,
    set_random_cover_from_category,
)

_logger = logging.getLogger(__name__)


@dataclass
class IllustrateOptions:
    """配图旋钮，跟 pipeline ai_illustrate 节点的 cfg 字段一一对应.

    max_images / min_spacing 的 0 等同 None（视为未设置，回退到风格默认 12/1 或 3/5）；
    要"无上限"请用 None；想要硬上限则传正整数。
    """

    include_companion: bool = True
    web_fallback: bool = False
    aggressive_images: bool = True
    max_images: int | None = None
    min_spacing: int | None = None
    preset_id: int | None = None
    set_cover: bool = True


@dataclass
class IllustrateResult:
    article_id: int
    images_inserted: int = 0
    cover_status: str = "skipped"
    cover_error: str | None = None
    format_error: str | None = None


def illustrate_one(
    *,
    article_id: int,
    main_category_id: int,
    user_id: int,
    options: IllustrateOptions,
    session_factory: Callable[[], Session],
) -> IllustrateResult:
    """给一篇文章配图 + 设封面，复用 pipeline ai_illustrate 节点的成熟逻辑.

    session_factory 而非 db：开两个独立短 session（配图持锁 / 封面独立提交），
    跟节点里的 _format_one + _maybe_set_cover 等价.

    异常传播：run_ai_format 内部捕获自身异常并写到 article.ai_format_error，
    正常情况下不抛出. 但若 run_ai_format 仍意外上抛（如 SDK 级 BaseException
    或新代码路径未覆盖的异常），本函数**不捕获**——异常会直接上抛到调用方，
    跳过阶段 2 (封面) 和阶段 3 (回读). 因此 HTTP endpoint / MCP tool 必须自己
    try/except 兜底，转成结构化的 IllustrateResult(format_error=str(exc)) 或
    500 响应，避免直接把内部 traceback 暴露给 LLM-facing 客户端.
    """
    aggressive = options.aggressive_images
    builtin_variant = "aggressive" if aggressive else "conservative"
    max_images = (
        options.max_images
        if (options.max_images and options.max_images > 0)
        else (12 if aggressive else 3)
    )
    min_spacing = (
        options.min_spacing
        if (options.min_spacing and options.min_spacing > 0)
        else (1 if aggressive else 5)
    )

    # 阶段 1: 配图 (持锁 + run_ai_format)
    lock_started_at = utcnow().replace(microsecond=0)
    candidate_categories: list = []

    db = session_factory()
    try:
        article = db.get(Article, article_id)
        if article is None or article.is_deleted:
            return IllustrateResult(
                article_id=article_id, format_error="article not found or deleted"
            )
        if not has_ai_format_targets(article.content_json):
            return IllustrateResult(
                article_id=article_id, format_error="no ai_format_targets in content"
            )
        candidate_categories = category_contexts_for(
            db,
            main_category_id=main_category_id,
            include_companion=options.include_companion,
        )
        article.ai_checking = True
        article.ai_checking_started_at = lock_started_at
        article.ai_format_error = None
        db.commit()
    finally:
        db.close()

    images_inserted = run_ai_format(
        article_id,
        include_images=True,
        lock_started_at=lock_started_at,
        preset_id=options.preset_id,
        user_id=user_id,
        candidate_categories=candidate_categories,
        web_fallback=options.web_fallback,
        max_images=max_images,
        min_spacing=min_spacing,
        builtin_variant=builtin_variant,
    )

    # 阶段 2: 封面 (独立短 session)
    cover_status = "skipped"
    cover_error: str | None = None
    if options.set_cover:
        db = session_factory()
        try:
            article = db.get(Article, article_id)
            if article is not None and not article.is_deleted:
                result: CoverResult = set_random_cover_from_category(
                    db, article, main_category_id, user_id
                )
                db.commit()
                cover_status = result.status
                cover_error = result.error
        except Exception as exc:  # noqa: BLE001 — best-effort
            db.rollback()
            cover_status = "error"
            cover_error = str(exc)
        finally:
            db.close()

    # 阶段 3: 回读 article.ai_format_error
    format_error: str | None = None
    db = session_factory()
    try:
        article = db.get(Article, article_id)
        if article is not None:
            format_error = article.ai_format_error
    finally:
        db.close()

    return IllustrateResult(
        article_id=article_id,
        images_inserted=images_inserted or 0,
        cover_status=cover_status,
        cover_error=cover_error,
        format_error=format_error,
    )
