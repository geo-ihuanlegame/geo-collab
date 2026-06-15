"""to_review 动作节点（「进入未审核库」）：把上游文章置 pending 并成一个组，输出 group_id。

输出 group_id 即向执行器表明「已成组」，执行器不再兜底成组（见 executor 的成组逻辑）。
daily_group=True 时按天归组：当天所有运行/流水线并入同一个「每日生成 · 日期」分组。"""

import datetime as dt
from zoneinfo import ZoneInfo

from server.app.core.config import get_settings
from server.app.modules.articles.service import (
    mark_pending_and_append_daily,
    mark_pending_and_group,
)
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register


def run_to_review(ctx: NodeRunContext) -> NodeResult:
    cfg = ctx.config or {}
    article_ids = ctx.inputs.get("article_ids") or cfg.get("article_ids") or []
    if not article_ids:
        return NodeResult(output={"skipped": "无文章"}, article_ids=[])

    # 守卫：上游已带 group_id（ai_generate 已流式成组）→ 透传，不再建新组。
    # 同查 inputs 与 upstream（防下游 inputMapping 把 group_id 字段筛掉）。
    already_gid = ctx.inputs.get("group_id") or (ctx.upstream or {}).get("group_id")
    if already_gid:
        # article_ids 透传在 output 里供下游 distribute 消费；NodeResult.article_ids=[] 与
        # daily/default 分支一致——执行器无需在此累计成组（上游流式已成组）。
        return NodeResult(
            output={"group_id": already_gid, "article_ids": list(article_ids)},
            article_ids=[],
        )

    if cfg.get("daily_group"):
        today = dt.datetime.now(ZoneInfo(get_settings().scheduler_tz)).date()
        gid = mark_pending_and_append_daily(
            ctx.session_factory,
            article_ids=list(article_ids),
            user_id=ctx.user_id,
            group_name=f"每日生成 · {today:%Y-%m-%d}",
        )
    else:
        base_name = (cfg.get("group_name") or "").strip() or "未审核 · 智能体生成"
        gid = mark_pending_and_group(
            ctx.session_factory,
            article_ids=list(article_ids),
            user_id=ctx.user_id,
            base_name=base_name,
            fallback_suffix=f"#{article_ids[0]}",
        )
    return NodeResult(output={"group_id": gid, "article_ids": list(article_ids)}, article_ids=[])


register("to_review", run_to_review)
