"""ai_generate 处理节点。

两种模式：
- 逐单元（上游问题源传入 generation_units）：每个单元解析模板/数量（缺则各自兜底到本节点配置），
  每篇从该单元允许模板里随机抽一个有效模板；模型用本节点 ai_engine（config["model"]）。
  总量受 ai_generate_max_count 约束；单篇/单元失败收进 errors，交由运行聚合为 partial_failed。
- 扁平（无 generation_units）：按本节点单模板 + 数量并发生成（原行为）。"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from server.app.modules.ai_generation.article_writer import generate_article_from_prompt
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.modules.pipelines.nodes.daily_group_stream import make_group_streamer
from server.app.modules.prompt_templates.service import get_visible_prompt_template
from server.app.shared.errors import ValidationError

logger = logging.getLogger(__name__)


def _resolve_units(units, fallback_template_id, fallback_count) -> list[tuple]:
    """generation_units → [(question_text, template_ids, count)]，模板/数量各自独立兜底。"""
    resolved: list[tuple] = []
    for u in units:
        if not isinstance(u, dict):
            continue
        qtext = (u.get("question_text") or "").strip()
        if not qtext:
            continue
        own_tpl_ids = list(u.get("allowed_prompt_template_ids") or [])
        tpl_ids = own_tpl_ids
        tpl_fallback = False
        if not tpl_ids and fallback_template_id:
            tpl_ids = [fallback_template_id]
            tpl_fallback = True
        raw_count = u.get("article_count")
        try:
            cnt = int(raw_count) if raw_count is not None else 0
        except (TypeError, ValueError):
            cnt = 0
        count_fallback = cnt <= 0
        if count_fallback:
            cnt = fallback_count
        # 诊断：逐单元记录最终用的模板/数量、以及是否回退到了本节点兜底。
        # 排查「per-type 没覆盖」时，tpl_fallback / count_fallback 为 True 即说明该单元
        # 没带 per-type 值（上游没发出/被截断），ai_generate 用了自身设置。
        logger.info(
            "ai_generate unit type=%s tpl_ids=%s(fallback=%s) count=%s(fallback=%s)",
            u.get("question_type"),
            tpl_ids,
            tpl_fallback,
            cnt,
            count_fallback,
        )
        resolved.append((qtext, tpl_ids, cnt))
    return resolved


def _run_units(ctx: NodeRunContext, cfg: dict, units, model, max_count) -> NodeResult:
    from server.app.modules.ai_generation.scheme_executor import _pick_valid_template

    fallback_template_id = cfg.get("prompt_template_id")
    fallback_count = int(cfg.get("count") or 0)
    resolved = _resolve_units(units, fallback_template_id, fallback_count)

    total = sum(c for (_, _, c) in resolved)
    if total <= 0:
        raise ValidationError(
            "ai_generate 逐单元：解析后总生成数量为 0（请在问题源或本节点配置数量）"
        )
    if total > max_count:
        raise ValidationError(f"生成数量超过上限 {max_count}")

    group_id, stream = make_group_streamer(ctx, cfg)
    article_ids: list[int] = []
    errors: list[str] = []

    def _one(qtext: str, tpl_ids: list[int]) -> int:
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
            except Exception as exc:  # 单篇失败不中断
                errors.append(str(exc))

    return NodeResult(
        output={"article_ids": article_ids, "errors": errors, "group_id": group_id},
        article_ids=article_ids,
    )


def run_ai_generate(ctx: NodeRunContext) -> NodeResult:
    from server.app.core.config import get_settings

    cfg = ctx.config or {}
    model = cfg.get("model")
    max_count = get_settings().ai_generate_max_count

    # 结构化交接字段（generation_units / question_text）应始终从问题源传到本节点。
    # 默认透传已能带过来；但若下游显式配了不含该字段的 inputMapping，会把它从 inputs 里筛掉。
    # 这里兜底回退到完整 upstream，避免 per-type 模板/数量被「字段映射」悄悄丢弃（见根因③）。
    upstream = ctx.upstream or {}
    units = ctx.inputs.get("generation_units") or upstream.get("generation_units")
    if units:
        # 诊断：确认走的是逐单元（per-type 生效）还是扁平（用本节点兜底）。
        logger.info("ai_generate mode=units (%d generation_units received)", len(units))
        return _run_units(ctx, cfg, units, model, max_count)
    logger.info(
        "ai_generate mode=flat (no generation_units; inputs_keys=%s upstream_keys=%s)",
        sorted(ctx.inputs.keys()),
        sorted(upstream.keys()),
    )

    # 扁平模式（原行为，未改动语义）
    question_text = (
        ctx.inputs.get("question_text")
        or upstream.get("question_text")
        or cfg.get("question_text")
        or ""
    )
    if not question_text:
        raise ValidationError("ai_generate 节点缺少 question_text（上游未传且未配置）")

    template_id = cfg.get("prompt_template_id")
    count = int(cfg.get("count") or 0)
    if not template_id or count <= 0:
        raise ValidationError("ai_generate 节点需配置 prompt_template_id 与 count>0")
    if count > max_count:
        raise ValidationError(f"生成数量超过上限 {max_count}")

    db = ctx.session_factory()
    try:
        tpl = get_visible_prompt_template(db, template_id, user_id=ctx.user_id, scope="generation")
        if tpl is None or not tpl.is_enabled:
            raise ValidationError("提示词模板无效（不存在/无权访问/停用/删除/非 generation）")
        template_content = tpl.content
    finally:
        db.close()

    group_id, stream = make_group_streamer(ctx, cfg)
    article_ids: list[int] = []
    errors: list[str] = []

    def _one() -> int:
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
            except Exception as exc:
                errors.append(str(exc))

    return NodeResult(
        output={"article_ids": article_ids, "errors": errors, "group_id": group_id},
        article_ids=article_ids,
    )


register("ai_generate", run_ai_generate)
