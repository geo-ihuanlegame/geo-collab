from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_distribute(ctx: NodeRunContext) -> NodeResult:
    from server.app.modules.system.models import User
    from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
    from server.app.modules.tasks.service import create_task

    cfg = ctx.config or {}
    group_id = ctx.inputs.get("group_id") or cfg.get("group_id")
    if not group_id:
        raise ValidationError("distribute 节点缺少 group_id（上游未传且未配置）")
    account_ids = cfg.get("account_ids") or []
    if not account_ids:
        raise ValidationError("distribute 节点需配置至少一个分发账号")
    name = cfg.get("name") or f"自动分发 分组 {group_id}"

    db = ctx.session_factory()
    try:
        user = db.get(User, ctx.user_id)
        role = user.role if user is not None else "operator"
        task_create = TaskCreate(
            name=name,
            task_type="group_round_robin",
            group_id=group_id,
            accounts=[
                TaskAccountInput(account_id=a, sort_order=i) for i, a in enumerate(account_ids)
            ],
            stop_before_publish=False,
        )
        # create_task 内部做审核门禁(_validate_articles_approved)+账号校验，抛命名异常
        task = create_task(db, ctx.user_id, task_create, role=role)
        db.commit()
        task_id = task.id
    finally:
        db.close()

    return NodeResult(output={"task_id": task_id}, article_ids=[])


register("distribute", run_distribute)
