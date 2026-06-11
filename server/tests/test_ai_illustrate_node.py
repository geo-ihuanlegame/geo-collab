import pytest

from server.tests.utils import build_test_app


def _make_category(app, name, bucket, kind):
    from server.app.modules.image_library.models import StockCategory

    with app.session_factory() as db:
        cat = StockCategory(name=name, bucket_name=bucket, kind=kind)
        db.add(cat)
        db.commit()
        db.refresh(cat)
        return cat.id


def _make_article(client):
    r = client.post(
        "/api/articles",
        json={
            "title": "配图测试",
            "content_json": {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "这是正文段落"}]}
                ],
            },
            "content_html": "<p>这是正文段落</p>",
            "plain_text": "这是正文段落",
            "word_count": 5,
            "status": "draft",
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _uid(app):
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        return db.query(User).first().id


@pytest.mark.mysql
def test_ai_illustrate_candidates_and_passthrough(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        main_id = _make_category(app, "主推A", "main-a", "main")
        comp_id = _make_category(app, "陪衬B", "comp-b", "companion")
        aid = _make_article(app.client)
        uid = _uid(app)

        captured: dict = {}

        def _stub(
            article_id,
            *,
            include_images,
            lock_started_at,
            preset_id,
            user_id,
            candidate_categories,
            web_fallback=False,
        ):
            captured["article_id"] = article_id
            captured["candidates"] = candidate_categories
            captured["include_images"] = include_images
            captured["web_fallback"] = web_fallback

        monkeypatch.setattr("server.app.modules.pipelines.nodes.ai_illustrate.run_ai_format", _stub)

        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        res = run_ai_illustrate(
            NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={"main_category_id": main_id, "include_companion": True},
                inputs={"article_ids": [aid]},
                upstream={},
            )
        )
        assert res.article_ids == [aid]
        assert captured["article_id"] == aid
        assert captured["include_images"] is True
        ids = {c["id"] for c in captured["candidates"]}
        assert main_id in ids and comp_id in ids
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_ai_illustrate_companion_toggle_off(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        main_id = _make_category(app, "主推A", "main-a", "main")
        comp_id = _make_category(app, "陪衬B", "comp-b", "companion")
        aid = _make_article(app.client)
        uid = _uid(app)
        captured: dict = {}
        monkeypatch.setattr(
            "server.app.modules.pipelines.nodes.ai_illustrate.run_ai_format",
            lambda article_id, **kw: captured.update(candidates=kw["candidate_categories"]),
        )
        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        run_ai_illustrate(
            NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={"main_category_id": main_id, "include_companion": False},
                inputs={"article_ids": [aid]},
                upstream={},
            )
        )
        ids = {c["id"] for c in captured["candidates"]}
        assert ids == {main_id} and comp_id not in ids
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_ai_illustrate_surfaces_image_count_and_swallowed_errors(monkeypatch):
    """节点须回传实际插图数与被 run_ai_format 吞掉的逐篇排版错误，

    否则 0 张图 / 全失败也显示成功（用户原始痛点：跑成功但没图、没提示）。
    """
    app = build_test_app(monkeypatch)
    try:
        main_id = _make_category(app, "主推A", "main-a", "main")
        ok_id = _make_article(app.client)
        fail_id = _make_article(app.client)
        uid = _uid(app)

        def _stub(article_id, **kw):
            # 模拟 run_ai_format：ok 篇配 2 张图；fail 篇配图失败、把错误写进 ai_format_error（不抛）
            from server.app.modules.articles.models import Article

            if article_id == fail_id:
                with app.session_factory() as db:
                    a = db.get(Article, fail_id)
                    a.ai_format_error = "AI 排版失败：DeepSeek 账户余额不足"
                    a.ai_checking = False
                    a.ai_checking_started_at = None
                    db.commit()
                return 0
            return 2

        monkeypatch.setattr("server.app.modules.pipelines.nodes.ai_illustrate.run_ai_format", _stub)

        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        res = run_ai_illustrate(
            NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={"main_category_id": main_id},
                inputs={"article_ids": [ok_id, fail_id]},
                upstream={},
            )
        )
        assert res.output["images_inserted"] == 2
        assert any(str(fail_id) in e and "余额不足" in e for e in res.output["format_errors"])
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_ai_illustrate_empty_inputs(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        uid = _uid(app)
        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        res = run_ai_illustrate(
            NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={"main_category_id": 1, "include_companion": True},
                inputs={"article_ids": []},
                upstream={},
            )
        )
        assert res.article_ids == []
    finally:
        app.cleanup()
