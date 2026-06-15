"""ai_compose 处理节点（前端「AI创作」）：平台主用的生文节点，两种模式。

- 逐单元（上游问题源传入 generation_units，且**至少一个类型显式配了文章数**）：按问题类型
  逐单元生文，每类用该类型自带的「允许模板 + 文章数」，缺则各自回退到本节点的
  prompt_template_ids / count。总量受 ai_generate_max_count 约束。
- 扁平（无 units，或没有任何类型配文章数=向后兼容护栏）：把上游问题文本整段喂入，按本节点
  prompt_template_ids（多选，运行时随机）× count 生成（原行为，未改语义）。

两种模式每篇都从允许模板里随机挑一个有效模板再生文，并发 max_workers=4；上游无问题时安静
跳过（不报错），单篇/单元失败收进 errors，交由运行聚合为 partial_failed。"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from server.app.modules.ai_generation.article_writer import generate_article_from_prompt
from server.app.modules.ai_generation.scheme_executor import _pick_valid_template
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.modules.pipelines.nodes.daily_group_stream import make_group_streamer
from server.app.shared.errors import ValidationError

logger = logging.getLogger(__name__)


def _coerce_count(v) -> int:
    """非正/非法 → 0（用于"显式数量"判定与回退）。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _any_unit_has_count(units) -> bool:
    """是否有任一单元显式配了 article_count>0——逐单元模式的开关（向后兼容护栏）：
    存量流程（含 question_source 旧扁平 config 派生的 gen_unit，其 article_count 恒为 None）
    不会被误切到逐单元，仍按本节点 count 走扁平。"""
    return any(
        isinstance(u, dict) and _coerce_count(u.get("article_count")) > 0 for u in (units or [])
    )


def _resolve_units(units, fallback_template_ids, fallback_count) -> list[tuple]:
    """generation_units → [(question_text, tpl_ids, count)]；模板/数量各自独立回退到本节点。

    与 ai_generate 不同：本节点兜底模板是**多选** prompt_template_ids（运行时随机），
    故 fallback_template_ids 是 list。"""
    resolved: list[tuple] = []
    for u in units:
        if not isinstance(u, dict):
            continue
        qtext = (u.get("question_text") or "").strip()
        if not qtext:
            continue
        tpl_ids = list(u.get("allowed_prompt_template_ids") or []) or list(
            fallback_template_ids or []
        )
        cnt = _coerce_count(u.get("article_count")) or fallback_count
        # 诊断：逐单元记录最终用的模板/数量，便于排查"per-type 没覆盖/回退到节点兜底"。
        logger.info(
            "ai_compose unit type=%s tpl_ids=%s count=%s", u.get("question_type"), tpl_ids, cnt
        )
        resolved.append((qtext, tpl_ids, cnt))
    return resolved


def _run_units(
    ctx, cfg, units, fallback_template_ids, fallback_count, model, max_count
) -> NodeResult:
    resolved = _resolve_units(units, fallback_template_ids, fallback_count)
    total = sum(c for (_, _, c) in resolved)
    if total <= 0:
        raise ValidationError(
            "ai_compose 逐单元：解析后总生成数量为 0（请在问题源或本节点配置数量）"
        )
    if total > max_count:
        raise ValidationError(f"生成数量超过上限 {max_count}")

    group_id, stream = make_group_streamer(ctx, cfg)
    article_ids: list[int] = []
    errors: list[str] = []

    def _one(qtext: str, tpl_ids: list[int]) -> int:
        # 每篇运行时从该单元允许模板里随机挑一个有效的（每线程自建会话）
        db = ctx.session_factory()
        try:
            tpl = _pick_valid_template(db, tpl_ids, ctx.user_id) if tpl_ids else None
            if tpl is None:
                raise ValidationError("该单元允许模板在运行时全部无效或未配置")
            template_content = tpl.content
        finally:
            db.close()
        aid = generate_article_from_prompt(
            session_factory=ctx.session_factory,
            user_id=ctx.user_id,
            template_content=template_content,
            question_text=qtext,
            model=model,
        )
        stream(aid)
        return aid

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(_one, qtext, tpl_ids)
            for (qtext, tpl_ids, cnt) in resolved
            for _ in range(cnt)
        ]
        for fut in as_completed(futures):
            try:
                article_ids.append(fut.result())
            except Exception as exc:  # 单篇/单元失败不中断，交由运行聚合为 partial_failed
                errors.append(str(exc))

    return NodeResult(
        output={"article_ids": article_ids, "errors": errors, "group_id": group_id},
        article_ids=article_ids,
    )


def run_ai_compose(ctx: NodeRunContext) -> NodeResult:
    from server.app.core.config import get_settings

    cfg = ctx.config or {}
    template_ids = cfg.get("prompt_template_ids") or []
    count = int(cfg.get("count") or 1)
    model = cfg.get("ai_engine")
    max_count = get_settings().ai_generate_max_count

    # 逐单元：上游问题源派下 generation_units 且至少一个类型显式配了文章数 → per-type 接管。
    # 默认透传带 generation_units 过来；显式 inputMapping 漏掉时从 upstream 兜底取回（同 ai_generate）。
    upstream = ctx.upstream or {}
    units = ctx.inputs.get("generation_units") or upstream.get("generation_units")
    if units and _any_unit_has_count(units):
        logger.info(
            "ai_compose mode=units (%d generation_units, per-type counts present)", len(units)
        )
        return _run_units(ctx, cfg, units, template_ids, count, model, max_count)

    # 扁平模式（原行为，未改语义）
    question_text = ctx.inputs.get("question_text") or cfg.get("question_text") or ""
    if not question_text.strip():
        # 上游无问题（如池暂空）→ 安静跳过，不报错（daily_group 不在此路径建组）
        return NodeResult(
            output={"article_ids": [], "errors": [], "skipped": "无问题可生成"}, article_ids=[]
        )

    if not template_ids:
        raise ValidationError("ai_compose 节点需配置至少一个提示词模板")
    if count > max_count:
        count = max_count
    if count <= 0:
        raise ValidationError("生成数量需 > 0")

    group_id, stream = make_group_streamer(ctx, cfg)
    article_ids: list[int] = []
    errors: list[str] = []

    def _one() -> int:
        # 每篇运行时从允许模板里随机挑一个有效的（每个线程自建会话）
        db = ctx.session_factory()
        try:
            tpl = _pick_valid_template(db, template_ids, ctx.user_id)
            if tpl is None:
                raise ValidationError("允许的提示词模板在运行时全部无效")
            template_content = tpl.content
        finally:
            db.close()
        aid = generate_article_from_prompt(
            session_factory=ctx.session_factory,
            user_id=ctx.user_id,
            template_content=template_content,
            question_text=question_text,
            model=model,
        )
        stream(aid)
        return aid

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_one) for _ in range(count)]
        for fut in as_completed(futures):
            try:
                article_ids.append(fut.result())
            except Exception as exc:  # 单篇失败不中断，交由运行聚合为 partial_failed
                errors.append(str(exc))

    return NodeResult(
        output={"article_ids": article_ids, "errors": errors, "group_id": group_id},
        article_ids=article_ids,
    )


register("ai_compose", run_ai_compose)
