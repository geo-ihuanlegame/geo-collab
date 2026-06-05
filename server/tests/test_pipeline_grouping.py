"""回归：scheme_executor._group_run_articles 委托 articles.mark_pending_and_group 后行为不变。

Task 7（整改计划）把 `_group_run_articles` 的 80 行复制实现换成对
`articles.service.mark_pending_and_group` 的薄封装。本测试直接单元测试 `_group_run_articles`：
手动建一个 GenerationSchemeRun（带 article_ids）+ 几篇默认 approved 文章，调用后断言文章被
标 review_status='pending' 且全部归入一个新建的 ArticleGroup —— 证明委托后产出一致。

自包含：用 build_test_app 取 session_factory / client / admin 用户。
"""

import pytest

from server.tests.utils import build_test_app


def _make_article(client, title: str) -> int:
    resp = client.post(
        "/api/articles",
        json={
            "title": title,
            "content_json": {"type": "doc", "content": []},
            "content_html": "<p>x</p>",
            "plain_text": "x",
            "word_count": 1,
            "status": "ready",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


@pytest.mark.mysql
def test_group_run_articles_marks_pending_and_groups(monkeypatch):
    from server.app.core.time import utcnow
    from server.app.modules.ai_generation.models import GenerationSchemeRun
    from server.app.modules.ai_generation.scheme_executor import _group_run_articles
    from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem

    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        a1 = _make_article(client, "甲")
        a2 = _make_article(client, "乙")

        # 新建文章默认 review_status='approved'；委托 helper 后应翻成 pending
        with test_app.session_factory() as db:
            uid = db.get(Article, a1).user_id
            assert db.get(Article, a1).review_status == "approved"
            assert db.get(Article, a2).review_status == "approved"

        # 构造真实方案运行链路（pool -> scheme -> run，满足外键约束）
        with test_app.session_factory() as db:
            from server.app.modules.ai_generation.models import GenerationScheme, QuestionPool

            pool = QuestionPool(user_id=uid, name="池")
            db.add(pool)
            db.flush()
            scheme = GenerationScheme(user_id=uid, pool_id=pool.id, name="方案A")
            db.add(scheme)
            db.flush()
            run = GenerationSchemeRun(
                scheme_id=scheme.id,
                user_id=uid,
                status="done",
                article_ids=[a1, a2],
                created_at=utcnow(),
            )
            db.add(run)
            db.commit()
            run_id = run.id

        _group_run_articles(run_id, test_app.session_factory)

        with test_app.session_factory() as db:
            # 1. 两篇文章都被标为 pending（未审核）
            assert db.get(Article, a1).review_status == "pending"
            assert db.get(Article, a2).review_status == "pending"

            # 2. 恰好新建了一个分组，且按 article_ids 顺序纳入两篇文章
            groups = db.query(ArticleGroup).filter(ArticleGroup.user_id == uid).all()
            assert len(groups) == 1
            gid = groups[0].id
            items = (
                db.query(ArticleGroupItem)
                .filter(ArticleGroupItem.group_id == gid)
                .order_by(ArticleGroupItem.sort_order.asc())
                .all()
            )
            assert [it.article_id for it in items] == [a1, a2]
    finally:
        test_app.cleanup()
