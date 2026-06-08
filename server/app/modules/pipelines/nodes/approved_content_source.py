from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register


def run_approved_content_source(ctx: NodeRunContext) -> NodeResult:
    from sqlalchemy import select

    from server.app.modules.articles.models import Article
    from server.app.modules.system.models import User
    from server.app.modules.tasks.models import PublishRecord

    cfg = ctx.config or {}
    limit = int(cfg.get("limit") or 20)
    limit = max(1, min(limit, 200))
    exclude_distributed = cfg.get("exclude_distributed")
    exclude_distributed = True if exclude_distributed is None else bool(exclude_distributed)

    db = ctx.session_factory()
    try:
        user = db.get(User, ctx.user_id)
        is_admin = user is not None and user.role == "admin"
        stmt = select(Article.id).where(
            Article.review_status == "approved",
            Article.is_deleted == False,  # noqa: E712
        )
        if not is_admin:
            stmt = stmt.where(Article.user_id == ctx.user_id)
        if exclude_distributed:
            stmt = stmt.where(Article.id.notin_(select(PublishRecord.article_id).distinct()))
        stmt = stmt.order_by(Article.updated_at.desc()).limit(limit)
        article_ids = [r[0] for r in db.execute(stmt).all()]
    finally:
        db.close()

    return NodeResult(output={"article_ids": article_ids}, article_ids=[])


register("approved_content_source", run_approved_content_source)
