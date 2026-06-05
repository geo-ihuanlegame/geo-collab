import pytest

from server.tests.utils import build_test_app


def _make_pool_with_items(app, items):
    """items: list[(category, question_text, source_active)]. 返回 pool_id + user_id。"""
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool

    with app.session_factory() as db:
        from server.app.modules.system.models import User

        uid = db.query(User).first().id
        pool = QuestionPool(user_id=uid, name="测试池")
        db.add(pool)
        db.flush()
        for i, (cat, text, active) in enumerate(items):
            db.add(
                QuestionItem(
                    pool_id=pool.id,
                    record_id=f"r{i}",
                    fields={},
                    category=cat,
                    question_text=text,
                    source_active=active,
                )
            )
        db.commit()
        return pool.id, uid


@pytest.mark.mysql
def test_question_source_picks_type_and_active(monkeypatch):
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.question_source import run_question_source

    app = build_test_app(monkeypatch)
    try:
        pool_id, uid = _make_pool_with_items(
            app,
            [
                ("美食", "怎么做红烧肉", True),
                ("美食", "怎么做糖醋排骨", True),
                ("旅游", "去哪玩", True),
                ("美食", "停用的问题", False),
            ],
        )
        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"pool_id": pool_id, "question_type": "美食"},
            inputs={},
            upstream={},
        )
        res = run_question_source(ctx)
        assert "红烧肉" in res.output["question_text"]
        assert "糖醋排骨" in res.output["question_text"]
        assert "去哪玩" not in res.output["question_text"]
        assert "停用" not in res.output["question_text"]
        assert res.output["question_count"] == 2
        # 无匹配类型 → 空 question_text，不报错
        ctx2 = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"pool_id": pool_id, "question_type": "不存在"},
            inputs={},
            upstream={},
        )
        assert run_question_source(ctx2).output["question_text"] == ""
    finally:
        app.cleanup()


def _make_gen_template(app, uid, content="写：", enabled=True):
    from server.app.modules.prompt_templates.models import PromptTemplate

    with app.session_factory() as db:
        t = PromptTemplate(
            name="模板", content=content, scope="generation", user_id=uid, is_enabled=enabled
        )
        db.add(t)
        db.commit()
        return t.id


@pytest.mark.mysql
def test_ai_compose_generates_with_random_template(monkeypatch):
    calls = {"n": 0}

    def _fake_generate(*, session_factory, user_id, template_content, question_text, model=None):
        import uuid

        from server.app.modules.articles.schemas import ArticleCreate
        from server.app.modules.articles.service import create_article

        calls["n"] += 1
        db = session_factory()
        try:
            art = create_article(
                db,
                user_id,
                ArticleCreate(
                    title=f"A{calls['n']}",
                    content_json={"type": "doc", "content": []},
                    content_html="<p>x</p>",
                    plain_text="x",
                    word_count=1,
                    client_request_id=str(uuid.uuid4()),
                ),
            )
            db.commit()
            return art.id
        finally:
            db.close()

    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt", _fake_generate
    )
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_compose import run_ai_compose
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        with app.session_factory() as db:
            from server.app.modules.system.models import User

            uid = db.query(User).first().id
        t1 = _make_gen_template(app, uid)
        t2 = _make_gen_template(app, uid)
        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"ai_engine": None, "prompt_template_ids": [t1, t2], "count": 3},
            inputs={"question_text": "1. 怎么做红烧肉"},
            upstream={},
        )
        res = run_ai_compose(ctx)
        assert len(res.output["article_ids"]) == 3
        assert res.article_ids == res.output["article_ids"]
        # 空问题 → skipped
        ctx_empty = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"prompt_template_ids": [t1], "count": 2},
            inputs={"question_text": ""},
            upstream={},
        )
        r2 = run_ai_compose(ctx_empty)
        assert r2.output["article_ids"] == [] and r2.output.get("skipped")
        # 模板全无效 → errors 有值、article_ids 空、不抛
        bad = _make_gen_template(app, uid, enabled=False)
        ctx_bad = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"prompt_template_ids": [bad], "count": 1},
            inputs={"question_text": "1. q"},
            upstream={},
        )
        r3 = run_ai_compose(ctx_bad)
        assert r3.output["article_ids"] == [] and r3.output["errors"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_to_review_marks_pending_and_groups(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        from server.app.modules.articles.models import Article, ArticleGroupItem
        from server.app.modules.pipelines.nodes.base import NodeRunContext
        from server.app.modules.pipelines.nodes.to_review import run_to_review

        def _mk(title):
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
            return r.json()["id"]

        a1, a2 = _mk("甲"), _mk("乙")
        with app.session_factory() as db:
            uid = db.query(Article).first().user_id
        ctx = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={"group_name": "今日生成"},
            inputs={"article_ids": [a1, a2]},
            upstream={},
        )
        res = run_to_review(ctx)
        gid = res.output["group_id"]
        assert gid is not None
        with app.session_factory() as db:
            assert db.get(Article, a1).review_status == "pending"
            assert db.get(Article, a2).review_status == "pending"
            items = db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == gid).all()
            assert {it.article_id for it in items} == {a1, a2}
        # 空 article_ids → skipped 不建组
        ctx_empty = NodeRunContext(
            session_factory=app.session_factory,
            user_id=uid,
            config={},
            inputs={"article_ids": []},
            upstream={},
        )
        assert run_to_review(ctx_empty).output.get("skipped")
    finally:
        app.cleanup()
