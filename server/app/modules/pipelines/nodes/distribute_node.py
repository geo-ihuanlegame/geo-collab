"""distribute 动作节点（「内容分发」）：把上游文章建成发布任务（create_task，内部带审核门禁）。

优先消费上游 article_ids（即便空也消费、跳过）而非 group_id——否则会重拉全组、丢弃「已审未分发」
子集导致重复发布 / 整批失败（#45，详见 run_distribute 内联注释）。"""

from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_distribute(ctx: NodeRunContext) -> NodeResult:
    from server.app.modules.system.models import User
    from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
    from server.app.modules.tasks.service import create_task

    cfg = ctx.config or {}
    account_ids = cfg.get("account_ids") or []
    if not account_ids:
        raise ValidationError("distribute 节点需配置至少一个分发账号")

    article_ids = ctx.inputs.get("article_ids")
    group_id = ctx.inputs.get("group_id") or cfg.get("group_id")

    accounts = [TaskAccountInput(account_id=a, sort_order=i) for i, a in enumerate(account_ids)]

    # 优先级：上游给了 article_ids（即便为空）→ 消费它，走 article_round_robin。
    # 必须优先 article_ids 而非 group_id：article_group_source 默认透传会同时带 group_id + article_ids，
    # 其 article_ids 是「已审+未分发」子集；若先判 group_id 走分组路径会重拉全组、丢弃该子集，
    # 导致已分发文章被重复发布、未审文章令整批失败（#45）。空子集表示无新内容，跳过是正确语义
    # （定时分发跑完后不该每轮变红）。仅当无上游 article_ids（手动配置 group_id）时才走分组路径，
    # 保留分组语义 + 空分组报错。
    if article_ids is not None:
        if not article_ids:
            return NodeResult(output={"skipped": "无可分发内容"}, article_ids=[])
        name = cfg.get("name") or f"自动分发 {len(article_ids)} 篇"
        task_create = TaskCreate(
            name=name,
            task_type="article_round_robin",
            article_ids=list(article_ids),
            accounts=accounts,
            stop_before_publish=False,
        )
    elif group_id:
        name = cfg.get("name") or f"自动分发 分组 {group_id}"
        task_create = TaskCreate(
            name=name,
            task_type="group_round_robin",
            group_id=group_id,
            accounts=accounts,
            stop_before_publish=False,
        )
    else:
        raise ValidationError("distribute 节点缺少 article_ids（上游）或 group_id（配置）")

    db = ctx.session_factory()
    try:
        user = db.get(User, ctx.user_id)
        role = user.role if user is not None else "operator"
        # create_task 内部做审核门禁(_validate_articles_approved)+账号校验，抛命名异常
        task = create_task(db, ctx.user_id, task_create, role=role)
        db.commit()
        task_id = task.id
    finally:
        db.close()

    return NodeResult(output={"task_id": task_id}, article_ids=[])


register("distribute", run_distribute)
