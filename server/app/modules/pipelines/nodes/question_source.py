from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_question_source(ctx: NodeRunContext) -> NodeResult:
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool
    from server.app.modules.system.models import User

    cfg = ctx.config or {}
    pool_id = cfg.get("pool_id")
    question_type = cfg.get("question_type")
    if not pool_id or not question_type:
        raise ValidationError("question_source 节点需配置 pool_id 与 question_type")

    db = ctx.session_factory()
    try:
        pool = db.get(QuestionPool, pool_id)
        if pool is None or getattr(pool, "is_deleted", False):
            raise ValidationError("问题池不存在")
        if pool.user_id != ctx.user_id:
            user = db.get(User, ctx.user_id)
            if user is None or user.role != "admin":
                raise ValidationError("无权访问该问题池")
        rows = (
            db.query(QuestionItem.question_text)
            .filter(
                QuestionItem.pool_id == pool_id,
                QuestionItem.category == question_type,
                QuestionItem.source_active.is_(True),
            )
            .order_by(QuestionItem.id.asc())
            .all()
        )
        texts = [(r[0] or "").strip() for r in rows if (r[0] or "").strip()]
    finally:
        db.close()

    rendered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, start=1))
    return NodeResult(
        output={"question_text": rendered, "question_count": len(texts)},
        article_ids=[],
    )


register("question_source", run_question_source)
