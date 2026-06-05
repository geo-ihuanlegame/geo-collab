from concurrent.futures import ThreadPoolExecutor, as_completed

from server.app.modules.ai_generation.article_writer import generate_article_from_prompt
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.modules.prompt_templates.service import get_visible_prompt_template
from server.app.shared.errors import ValidationError


def run_ai_generate(ctx: NodeRunContext) -> NodeResult:
    cfg = ctx.config or {}
    # question_text：优先来自上游注入，其次 config 兜底
    question_text = ctx.inputs.get("question_text") or cfg.get("question_text") or ""
    if not question_text:
        raise ValidationError("ai_generate 节点缺少 question_text（上游未传且未配置）")

    template_id = cfg.get("prompt_template_id")
    count = int(cfg.get("count") or 0)
    model = cfg.get("model")
    if not template_id or count <= 0:
        raise ValidationError("ai_generate 节点需配置 prompt_template_id 与 count>0")

    from server.app.core.config import get_settings

    if count > get_settings().ai_generate_max_count:
        raise ValidationError(f"生成数量超过上限 {get_settings().ai_generate_max_count}")

    db = ctx.session_factory()
    try:
        tpl = get_visible_prompt_template(db, template_id, user_id=ctx.user_id, scope="generation")
        if tpl is None or not tpl.is_enabled:
            raise ValidationError("提示词模板无效（不存在/无权访问/停用/删除/非 generation）")
        template_content = tpl.content
    finally:
        db.close()

    article_ids: list[int] = []
    errors: list[str] = []

    def _one() -> int:
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
        output={"article_ids": article_ids, "errors": errors},
        article_ids=article_ids,
    )


register("ai_generate", run_ai_generate)
