from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_article_group_source(ctx: NodeRunContext) -> NodeResult:
    from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem

    group_id = (ctx.config or {}).get("group_id")
    if not group_id:
        raise ValidationError("article_group_source 节点需配置 group_id")

    db = ctx.session_factory()
    try:
        group = db.get(ArticleGroup, group_id)
        if group is None or group.is_deleted:
            raise ValidationError("分组不存在")
        if group.user_id != ctx.user_id:
            # admin 放行（与其它模块一致：role 需从 user 取）
            from server.app.modules.system.models import User

            user = db.get(User, ctx.user_id)
            if user is None or user.role != "admin":
                raise ValidationError("无权访问该分组")
        rows = (
            db.query(ArticleGroupItem.article_id)
            .join(Article, Article.id == ArticleGroupItem.article_id)
            .filter(ArticleGroupItem.group_id == group_id, Article.is_deleted.is_(False))
            .order_by(ArticleGroupItem.sort_order.asc())
            .all()
        )
        article_ids = [r[0] for r in rows]
    finally:
        db.close()

    return NodeResult(output={"group_id": group_id, "article_ids": article_ids}, article_ids=[])


register("article_group_source", run_article_group_source)
