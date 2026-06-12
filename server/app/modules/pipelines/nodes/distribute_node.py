"""distribute 动作节点（「内容分发」）：把上游文章建成发布任务（create_task，内部带审核门禁）。

账号选择走「平台动态规则 + 账号级增减」模型（config.account_selection）：
  - platforms：选中的平台 code → 该平台**全部「已启用 + 有效」账号**（含未来新增账号，运行时解析）。
  - extra_account_ids：在平台规则之外单独追加的账号（可跨平台）。
  - excluded_account_ids：在平台规则内单独排除的账号。
解析后**按平台分组**，每个平台各建一个发布任务（受「一个任务=单平台」约束，service.py:_validated_accounts）。
兼容旧扁平 config.account_ids（视作 extra_account_ids）。

优先消费上游 article_ids（即便空也消费、跳过）而非 group_id——否则会重拉全组、丢弃「已审未分发」
子集导致重复发布 / 整批失败（#45，详见下方内联注释）。"""

from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def _selection_from_config(cfg: dict) -> dict:
    """归一化账号选择配置。新 account_selection 优先；否则兼容旧扁平 account_ids（=额外追加）。"""
    sel = cfg.get("account_selection")
    if isinstance(sel, dict):
        return {
            "platforms": list(sel.get("platforms") or []),
            "extra_account_ids": list(sel.get("extra_account_ids") or []),
            "excluded_account_ids": list(sel.get("excluded_account_ids") or []),
        }
    legacy = cfg.get("account_ids") or []
    return {"platforms": [], "extra_account_ids": list(legacy), "excluded_account_ids": []}


def resolve_distribution_accounts(
    db, selection: dict, *, user_id: int | None = None, role: str = "operator"
) -> list[tuple[str, list[int]]]:
    """把 {platforms, extra_account_ids, excluded_account_ids} 解析成「可发布账号、按平台分组」。

    动态规则：platforms 命中平台的**全部 distribution_enabled + status=valid** 账号（含未来新增），
    并入 extra_account_ids（同样过滤），再减去 excluded_account_ids。operator 只见自己账号，admin 见全部。
    返回 [(platform_code, [account_id, ...])]，按平台 code 升序、账号 id 升序，便于稳定派号与测试。
    """
    from sqlalchemy import or_, select

    from server.app.modules.accounts.models import Account
    from server.app.modules.system.models import Platform

    platforms = [str(p) for p in (selection.get("platforms") or [])]
    extra_ids = {int(a) for a in (selection.get("extra_account_ids") or [])}
    excluded_ids = {int(a) for a in (selection.get("excluded_account_ids") or [])}

    selectors = []
    if platforms:
        selectors.append(Platform.code.in_(platforms))
    if extra_ids:
        selectors.append(Account.id.in_(extra_ids))
    if not selectors:
        return []

    conds = [
        Account.is_deleted == False,  # noqa: E712
        Account.distribution_enabled == True,  # noqa: E712
        Account.status == "valid",
        or_(*selectors),
    ]
    owner = None if role == "admin" else user_id
    if owner is not None:
        conds.append(Account.user_id == owner)

    rows = db.execute(
        select(Account.id, Platform.code)
        .join(Platform, Account.platform_id == Platform.id)
        .where(*conds)
    ).all()

    grouped: dict[str, list[int]] = {}
    for account_id, platform_code in rows:
        if account_id in excluded_ids:
            continue
        grouped.setdefault(platform_code, []).append(account_id)
    return [(code, sorted(grouped[code])) for code in sorted(grouped)]


def run_distribute(ctx: NodeRunContext) -> NodeResult:
    from server.app.modules.system.models import User
    from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
    from server.app.modules.tasks.service import create_task

    cfg = ctx.config or {}
    selection = _selection_from_config(cfg)
    if not selection["platforms"] and not selection["extra_account_ids"]:
        raise ValidationError("distribute 节点需配置至少一个分发平台或账号")

    article_ids = ctx.inputs.get("article_ids")
    group_id = ctx.inputs.get("group_id") or cfg.get("group_id")

    # 优先级：上游给了 article_ids（即便空也消费、跳过）而非 group_id——否则会重拉全组、丢弃「已审未分发」
    # 子集导致重复发布 / 整批失败（#45）。仅当无上游 article_ids（手动配置 group_id）时才走分组路径。
    article_list: list[int] = []
    if article_ids is not None:
        if not article_ids:
            return NodeResult(output={"skipped": "无可分发内容"}, article_ids=[])
        article_list = list(article_ids)
        task_type = "article_round_robin"
    elif group_id:
        task_type = "group_round_robin"
    else:
        raise ValidationError("distribute 节点缺少 article_ids（上游）或 group_id（配置）")

    db = ctx.session_factory()
    try:
        user = db.get(User, ctx.user_id)
        role = user.role if user is not None else "operator"
        groups = resolve_distribution_accounts(db, selection, user_id=ctx.user_id, role=role)
        if not groups:
            # 配置了平台/账号，但当下无「已启用 + 有效」账号（全停用 / 全排除）→ 安静跳过，不报错。
            return NodeResult(output={"skipped": "无启用的分发账号"}, article_ids=[])

        base_name = cfg.get("name")
        multi = len(groups) > 1
        task_ids: list[int] = []
        for platform_code, account_ids in groups:
            accounts = [
                TaskAccountInput(account_id=a, sort_order=i) for i, a in enumerate(account_ids)
            ]
            if task_type == "article_round_robin":
                name = base_name or f"自动分发 {len(article_list)} 篇"
                task_create = TaskCreate(
                    name=f"{name} · {platform_code}" if multi else name,
                    task_type="article_round_robin",
                    platform_code=platform_code,
                    article_ids=list(article_list),
                    accounts=accounts,
                    stop_before_publish=False,
                )
            else:
                name = base_name or f"自动分发 分组 {group_id}"
                task_create = TaskCreate(
                    name=f"{name} · {platform_code}" if multi else name,
                    task_type="group_round_robin",
                    platform_code=platform_code,
                    group_id=group_id,
                    accounts=accounts,
                    stop_before_publish=False,
                )
            # create_task 内部做审核门禁（_validate_articles_approved）+ 账号校验，抛命名异常
            task = create_task(db, ctx.user_id, task_create, role=role)
            task_ids.append(task.id)
        db.commit()
    finally:
        db.close()

    # 单平台时同时回传 task_id（向后兼容旧消费方/日志），多平台用 task_ids 全量。
    out: dict = {"task_ids": task_ids}
    if len(task_ids) == 1:
        out["task_id"] = task_ids[0]
    return NodeResult(output=out, article_ids=[])


register("distribute", run_distribute)
