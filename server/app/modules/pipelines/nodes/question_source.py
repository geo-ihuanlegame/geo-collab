from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_question_source(ctx: NodeRunContext) -> NodeResult:
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool
    from server.app.modules.system.models import User

    cfg = ctx.config or {}
    pool_id = cfg.get("pool_id")
    # question_type 可选：空 → 取整池（"全部类型"）；"__uncategorized__" → 仅未分类（category 为 NULL）；
    # 其它 → 按该具体分类过滤。
    question_type = cfg.get("question_type")
    if not pool_id:
        raise ValidationError("question_source 节点需配置 pool_id")

    db = ctx.session_factory()
    try:
        pool = db.get(QuestionPool, pool_id)
        if pool is None or getattr(pool, "is_deleted", False):
            raise ValidationError("问题池不存在")
        if pool.user_id != ctx.user_id:
            user = db.get(User, ctx.user_id)
            if user is None or user.role != "admin":
                raise ValidationError("无权访问该问题池")
        query = db.query(QuestionItem.question_text).filter(
            QuestionItem.pool_id == pool_id,
            QuestionItem.source_active.is_(True),
        )
        if question_type == "__uncategorized__":
            query = query.filter(QuestionItem.category.is_(None))
        elif question_type:
            query = query.filter(QuestionItem.category == question_type)
        # else: 空 question_type → 不按类型过滤，取整池
        rows = query.order_by(QuestionItem.id.asc()).all()
        texts = [(r[0] or "").strip() for r in rows if (r[0] or "").strip()]
    finally:
        db.close()

    rendered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, start=1))
    return NodeResult(
        output={"question_text": rendered, "question_count": len(texts)},
        article_ids=[],
    )


register("question_source", run_question_source)
