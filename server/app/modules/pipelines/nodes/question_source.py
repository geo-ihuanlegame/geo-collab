"""question_source 源节点：从问题池按「精选问题 record_ids > 多选类型 > 整池」取问题，

拼成带序号的多行 question_text 供下游生文。兼容旧单选 question_type；
__uncategorized__ 表示「未分类」(category 为 None)。"""

from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_question_source(ctx: NodeRunContext) -> NodeResult:
    from sqlalchemy import or_

    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool
    from server.app.modules.system.models import User

    cfg = ctx.config or {}
    pool_id = cfg.get("pool_id")
    if not pool_id:
        raise ValidationError("question_source 节点需配置 pool_id")

    # 多选类型；向后兼容旧单选 question_type
    question_types = cfg.get("question_types")
    if question_types is None:
        legacy = cfg.get("question_type")
        question_types = [] if (legacy is None or legacy == "") else [legacy]
    question_record_ids = cfg.get("question_record_ids") or []

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
        if question_record_ids:
            # 精选：record_id 命中且仍有效；失效/不存在的自动跳过
            query = query.filter(QuestionItem.record_id.in_(question_record_ids))
        elif question_types:
            named = [t for t in question_types if t != "__uncategorized__"]
            conds = []
            if named:
                conds.append(QuestionItem.category.in_(named))
            if "__uncategorized__" in question_types:
                conds.append(QuestionItem.category.is_(None))
            if conds:
                query = query.filter(or_(*conds))
        # 未配置筛选：不过滤，取整池
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
