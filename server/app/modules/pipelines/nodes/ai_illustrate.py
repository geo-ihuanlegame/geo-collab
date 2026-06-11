"""ai_illustrate 处理节点（前端「AI配图」）：给上游文章自动配图。

复用 articles.ai_format.run_ai_format：把「主推栏目 + (可选)全部陪衬栏目」作为候选栏目
喂给 AI格式 模型，由模型按文章内容决定插哪几张、插哪里（决策：主推+陪衬统一交 AI 决定）。
并发 max_workers=4，每篇独立置 ai_checking 锁（照 scheme_executor 成熟调用法）；
单篇失败收进 errors（partial_failed），不中断。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from server.app.core.time import utcnow
from server.app.modules.articles.ai_format import (
    category_contexts_for,
    has_ai_format_targets,
    run_ai_format,
)
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_ai_illustrate(ctx: NodeRunContext) -> NodeResult:
    cfg = ctx.config or {}
    article_ids = [a for a in (ctx.inputs.get("article_ids") or []) if isinstance(a, int)]
    if not article_ids:
        return NodeResult(
            output={"article_ids": [], "errors": [], "skipped": "无文章可配图"}, article_ids=[]
        )

    main_category_id = cfg.get("main_category_id")
    if not isinstance(main_category_id, int):
        raise ValidationError("ai_illustrate 节点需配置主推栏目 main_category_id")
    include_companion = bool(cfg.get("include_companion", True))
    web_fallback = bool(cfg.get("web_fallback", False))
    cfg_preset_id = cfg.get("preset_id")

    errors: list[str] = []

    def _one(article_id: int) -> int:
        from server.app.modules.articles.models import Article
        from server.app.modules.system.models import User

        lock_started_at = utcnow().replace(microsecond=0)
        candidate_categories: list[Any] = []
        effective_preset: int | None = None

        db = ctx.session_factory()
        try:
            article = db.get(Article, article_id)
            if article is None or article.is_deleted:
                return 0
            if not has_ai_format_targets(article.content_json):
                return 0
            user = db.get(User, ctx.user_id)
            effective_preset = (
                cfg_preset_id
                if isinstance(cfg_preset_id, int)
                else (getattr(user, "ai_format_preset_id", None) if user else None)
            )
            candidate_categories = category_contexts_for(
                db, main_category_id=main_category_id, include_companion=include_companion
            )
            article.ai_checking = True
            article.ai_checking_started_at = lock_started_at
            article.ai_format_error = None
            db.commit()
        finally:
            db.close()

        return run_ai_format(
            article_id,
            include_images=True,
            lock_started_at=lock_started_at,
            preset_id=effective_preset,
            user_id=ctx.user_id,
            candidate_categories=candidate_categories,
            web_fallback=web_fallback,
        )

    images_inserted = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_one, aid): aid for aid in article_ids}
        for fut in as_completed(futures):
            try:
                images_inserted += fut.result() or 0
            except Exception as exc:  # 单篇失败不中断，交由运行聚合为 partial_failed
                errors.append(f"article {futures[fut]}: {exc}")

    # run_ai_format 会吞掉自身异常并把详情写进 article.ai_format_error（不会进上面的 errors）。
    # 回读出来一并暴露，否则配图失败 / 0 张图也只显示成功——正是用户原始痛点。
    format_errors = _collect_format_errors(ctx, article_ids)

    return NodeResult(
        output={
            "article_ids": article_ids,
            "errors": errors,
            "images_inserted": images_inserted,
            "format_errors": format_errors,
        },
        article_ids=article_ids,
    )


def _collect_format_errors(ctx: NodeRunContext, article_ids: list[int]) -> list[str]:
    """回读各文章被 run_ai_format 吞掉的 ai_format_error，拼成 'article {id}: {错误}' 列表。"""
    from server.app.modules.articles.models import Article

    out: list[str] = []
    db = ctx.session_factory()
    try:
        for aid in article_ids:
            article = db.get(Article, aid)
            err = getattr(article, "ai_format_error", None) if article is not None else None
            if err:
                out.append(f"article {aid}: {err}")
    finally:
        db.close()
    return out


register("ai_illustrate", run_ai_illustrate)
