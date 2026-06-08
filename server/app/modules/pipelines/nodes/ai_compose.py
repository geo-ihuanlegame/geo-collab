"""ai_compose 处理节点（前端「AI创作」）：与 ai_generate 类似但允许多个提示词模板，

每篇运行时从允许集合里随机挑一个有效模板再生文，并发 max_workers=4。
问题文本（上游输出，缺则取 config.question_text 兜底）为空时安静跳过（不报错），单篇失败收进 errors 交 run 聚合 partial_failed。"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from server.app.modules.ai_generation.article_writer import generate_article_from_prompt
from server.app.modules.ai_generation.scheme_executor import _pick_valid_template
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_ai_compose(ctx: NodeRunContext) -> NodeResult:
    cfg = ctx.config or {}
    question_text = ctx.inputs.get("question_text") or cfg.get("question_text") or ""
    if not question_text.strip():
        # 上游无问题（如池暂空）→ 安静跳过，不报错
        return NodeResult(
            output={"article_ids": [], "errors": [], "skipped": "无问题可生成"}, article_ids=[]
        )

    template_ids = cfg.get("prompt_template_ids") or []
    if not template_ids:
        raise ValidationError("ai_compose 节点需配置至少一个提示词模板")
    count = int(cfg.get("count") or 1)
    model = cfg.get("ai_engine")

    from server.app.core.config import get_settings

    max_count = get_settings().ai_generate_max_count
    if count > max_count:
        count = max_count
    if count <= 0:
        raise ValidationError("生成数量需 > 0")

    article_ids: list[int] = []
    errors: list[str] = []

    def _one() -> int:
        # 每篇运行时从允许模板里随机挑一个有效的（每线程自建 session）
        db = ctx.session_factory()
        try:
            tpl = _pick_valid_template(db, template_ids, ctx.user_id)
            if tpl is None:
                raise ValidationError("允许的提示词模板在运行时全部无效")
            template_content = tpl.content
        finally:
            db.close()
        return generate_article_from_prompt(
            session_factory=ctx.session_factory,
            user_id=ctx.user_id,
            template_content=template_content,
            question_text=question_text,
            model=model,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_one) for _ in range(count)]
        for fut in as_completed(futures):
            try:
                article_ids.append(fut.result())
            except Exception as exc:  # 单篇失败不中断，交由 run 聚合 partial_failed
                errors.append(str(exc))

    return NodeResult(
        output={"article_ids": article_ids, "errors": errors}, article_ids=article_ids
    )


register("ai_compose", run_ai_compose)
