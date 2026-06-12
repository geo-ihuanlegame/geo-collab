import pytest

from server.tests.utils import build_test_app


def _make_article(client, title="文章"):
    r = client.post(
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
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@pytest.mark.mysql
def test_append_daily_accumulates_and_dedups(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem
    from server.app.modules.articles.service import mark_pending_and_append_daily

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1, a2, a3 = (_make_article(client, t) for t in ("甲", "乙", "丙"))
        with app.session_factory() as db:
            uid = db.get(Article, a1).user_id

        name = "每日生成 · 2026-06-11"
        gid1 = mark_pending_and_append_daily(
            app.session_factory, article_ids=[a1, a2], user_id=uid, group_name=name
        )
        # 第二次：含重复的 a2 + 新的 a3 → 复用同组、去重
        gid2 = mark_pending_and_append_daily(
            app.session_factory, article_ids=[a2, a3], user_id=uid, group_name=name
        )

        assert gid1 is not None and gid1 == gid2  # 同一个日期分组
        with app.session_factory() as db:
            groups = (
                db.query(ArticleGroup)
                .filter(ArticleGroup.user_id == uid, ArticleGroup.is_deleted == False)  # noqa: E712
                .all()
            )
            assert len(groups) == 1  # 只一个组
            items = db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == gid1).all()
            assert {it.article_id for it in items} == {a1, a2, a3}  # 三篇、不重复
            assert len(items) == 3
            for aid in (a1, a2, a3):
                assert db.get(Article, aid).review_status == "pending"  # 全部置 pending
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_append_daily_different_name_makes_new_group(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroup
    from server.app.modules.articles.service import mark_pending_and_append_daily

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1, a2 = (_make_article(client, t) for t in ("甲", "乙"))
        with app.session_factory() as db:
            uid = db.get(Article, a1).user_id
        g1 = mark_pending_and_append_daily(
            app.session_factory, article_ids=[a1], user_id=uid, group_name="每日生成 · 2026-06-11"
        )
        g2 = mark_pending_and_append_daily(
            app.session_factory, article_ids=[a2], user_id=uid, group_name="每日生成 · 2026-06-12"
        )
        assert g1 != g2  # 跨天 → 两个组
        with app.session_factory() as db:
            cnt = (
                db.query(ArticleGroup)
                .filter(ArticleGroup.user_id == uid, ArticleGroup.is_deleted == False)  # noqa: E712
                .count()
            )
            assert cnt == 2
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_to_review_daily_group_accumulates(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.to_review import run_to_review

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1, a2, a3 = (_make_article(client, t) for t in ("甲", "乙", "丙"))
        with app.session_factory() as db:
            uid = db.get(Article, a1).user_id

        def _ctx(ids):
            return NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={"daily_group": True},
                inputs={"article_ids": ids},
                upstream={},
            )

        r1 = run_to_review(_ctx([a1, a2]))
        r2 = run_to_review(_ctx([a3]))  # 同一天第二次运行
        assert r1.output["group_id"] == r2.output["group_id"]  # 累加进同组
        with app.session_factory() as db:
            groups = (
                db.query(ArticleGroup)
                .filter(ArticleGroup.user_id == uid, ArticleGroup.is_deleted == False)  # noqa: E712
                .all()
            )
            assert len(groups) == 1
            assert groups[0].name.startswith("每日生成 · ")
            items = (
                db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == groups[0].id).all()
            )
            assert {it.article_id for it in items} == {a1, a2, a3}
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_to_review_default_makes_new_group_each_run(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroup
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.to_review import run_to_review

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1, a2 = (_make_article(client, t) for t in ("甲", "乙"))
        with app.session_factory() as db:
            uid = db.get(Article, a1).user_id

        def _ctx(ids):
            return NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={},
                inputs={"article_ids": ids},
                upstream={},
            )

        r1 = run_to_review(_ctx([a1]))
        r2 = run_to_review(_ctx([a2]))
        assert r1.output["group_id"] != r2.output["group_id"]  # 默认每次新组（现状不变）
        with app.session_factory() as db:
            cnt = (
                db.query(ArticleGroup)
                .filter(ArticleGroup.user_id == uid, ArticleGroup.is_deleted == False)  # noqa: E712
                .count()
            )
            assert cnt == 2
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_node_types_to_review_has_daily_group_toggle(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        r = app.client.get("/api/pipelines/node-types")
        assert r.status_code == 200, r.text
        types = {nt["type"]: nt for nt in r.json()["node_types"]}
        fields = {f["key"]: f for f in types["to_review"]["config_schema"]}
        assert "daily_group" in fields
        assert fields["daily_group"]["type"] == "toggle"
    finally:
        app.cleanup()
