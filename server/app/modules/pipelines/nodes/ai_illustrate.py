"""ai_illustrate 处理节点（前端「AI配图」）：给上游文章自动配图.

复用 articles/ai_illustrate_svc.py 的 illustrate_one——pipeline 节点和 /goal
MCP loop 都调它，保证两条路径配图效果一致.

并发 max_workers=4，单篇失败收进 errors（partial_failed），不中断.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from server.app.core.logging import submit_in_context
from server.app.modules.articles.ai_illustrate_svc import (
    IllustrateOptions,
    IllustrateResult,
    illustrate_one,
)
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def _pos_int(v: Any, default: int) -> int:
    """正整数配置取值：非法 / 非正（含前端清空字段得到的 0）→ default。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


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
    set_cover = bool(cfg.get("set_cover", True))

    # 配图风格 / 数量旋钮（节点自包含，不再回退 user.ai_format_preset_id）：
    # aggressive_images 默认开 → 用「积极配图」内置变体（每个明确出现的游戏都插）；关 → 保守变体。
    # max_images / min_spacing 缺省随风格取激进(12/1) 或保守(3/5)，也作为插图阶段硬上限。
    # 运营若在「提示词管理」自建 ai_format 模板，可经 preset_id 覆盖措辞，数字旋钮与硬上限照样生效。
    aggressive = bool(cfg.get("aggressive_images", True))
    max_images = _pos_int(cfg.get("max_images"), 12 if aggressive else 3)
    min_spacing = _pos_int(cfg.get("min_spacing"), 1 if aggressive else 5)
    cfg_preset_id = cfg.get("preset_id")
    effective_preset = cfg_preset_id if isinstance(cfg_preset_id, int) else None

    errors: list[str] = []

    def _one(article_id: int) -> IllustrateResult:
        return illustrate_one(
            article_id=article_id,
            main_category_id=main_category_id,
            user_id=ctx.user_id,
            options=IllustrateOptions(
                include_companion=include_companion,
                web_fallback=web_fallback,
                aggressive_images=aggressive,
                max_images=max_images,
                min_spacing=min_spacing,
                preset_id=effective_preset,
                set_cover=set_cover,
            ),
            session_factory=ctx.session_factory,
        )

    images_inserted = 0
    covers_set = 0
    cover_errors: list[str] = []
    format_errors_from_results: list[str] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {submit_in_context(pool, _one, aid): aid for aid in article_ids}
        for fut in as_completed(futures):
            try:
                result = fut.result()
                images_inserted += result.images_inserted
                if result.cover_status == "set":
                    covers_set += 1
                elif result.cover_status == "error" and result.cover_error:
                    cover_errors.append(f"article {result.article_id}: {result.cover_error}")
                if result.format_error:
                    format_errors_from_results.append(
                        f"article {result.article_id}: {result.format_error}"
                    )
            except Exception as exc:  # 单篇未捕获异常不中断
                errors.append(f"article {futures[fut]}: {exc}")

    return NodeResult(
        output={
            "article_ids": article_ids,
            "errors": errors,
            "images_inserted": images_inserted,
            "format_errors": format_errors_from_results,
            "covers_set": covers_set,
            "cover_errors": cover_errors,
        },
        article_ids=article_ids,
    )


register("ai_illustrate", run_ai_illustrate)
