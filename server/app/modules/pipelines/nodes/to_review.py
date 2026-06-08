"""to_review 动作节点（「进入未审核库」）：把上游文章置 pending 并成一个组，输出 group_id。

输出 group_id 即向执行器表明「已成组」，执行器不再兜底成组（见 executor 的 grouped 逻辑）。"""

from server.app.modules.articles.service import mark_pending_and_group
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register


def run_to_review(ctx: NodeRunContext) -> NodeResult:
    cfg = ctx.config or {}
    article_ids = ctx.inputs.get("article_ids") or cfg.get("article_ids") or []
    if not article_ids:
        return NodeResult(output={"skipped": "无文章"}, article_ids=[])

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
