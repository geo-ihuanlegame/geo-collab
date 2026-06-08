from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_article_group_source(ctx: NodeRunContext) -> NodeResult:
    from sqlalchemy import select

    from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem
    from server.app.modules.system.models import User
    from server.app.modules.tasks.models import PublishRecord

    cfg = ctx.config or {}
    # group_id 可选：上游注入 > 节点配置 > 空(=自动按 FIFO 选组)
    configured_group_id = ctx.inputs.get("group_id") or cfg.get("group_id")

    db = ctx.session_factory()
    try:
        user = db.get(User, ctx.user_id)
        is_admin = user is not None and user.role == "admin"

        # 候选文章 = 已审核 + 未删 + 未分发(无 PublishRecord) + owner/admin
        def _candidate_filters(stmt):
            stmt = stmt.where(
                Article.review_status == "approved",
                Article.is_deleted == False,  # noqa: E712
                Article.id.notin_(select(PublishRecord.article_id)),
            )
            if not is_admin:
                stmt = stmt.where(Article.user_id == ctx.user_id)
            return stmt

        if configured_group_id:
            group = db.get(ArticleGroup, configured_group_id)
            if group is None or group.is_deleted:
                raise ValidationError("分组不存在")
            if group.user_id != ctx.user_id and not is_admin:
                raise ValidationError("无权访问该分组")
            chosen_group_id = configured_group_id
        else:
            # 自动 FIFO：含 ≥1 篇候选文章、未删、owner/admin 的最早分组
            grp_stmt = (
                select(ArticleGroup.id)
                .join(ArticleGroupItem, ArticleGroupItem.group_id == ArticleGroup.id)
                .join(Article, Article.id == ArticleGroupItem.article_id)
                .where(ArticleGroup.is_deleted == False)  # noqa: E712
            )
            grp_stmt = _candidate_filters(grp_stmt)
            if not is_admin:
                grp_stmt = grp_stmt.where(ArticleGroup.user_id == ctx.user_id)
            grp_stmt = grp_stmt.order_by(
                ArticleGroup.created_at.asc(), ArticleGroup.id.asc()
            ).limit(1)
            chosen_group_id = db.execute(grp_stmt).scalars().first()

        if chosen_group_id is None:
            return NodeResult(output={"group_id": None, "article_ids": []}, article_ids=[])

        art_stmt = (
            select(ArticleGroupItem.article_id)
            .join(Article, Article.id == ArticleGroupItem.article_id)
            .where(ArticleGroupItem.group_id == chosen_group_id)
        )
        art_stmt = _candidate_filters(art_stmt)
        art_stmt = art_stmt.order_by(ArticleGroupItem.sort_order.asc())
        article_ids = list(db.execute(art_stmt).scalars().all())
    finally:
        db.close()

    return NodeResult(
        output={"group_id": chosen_group_id, "article_ids": article_ids}, article_ids=[]
    )


register("article_group_source", run_article_group_source)
