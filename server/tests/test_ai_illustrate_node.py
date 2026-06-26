import base64

import pytest

from server.tests.utils import build_test_app

# 1x1 透明 PNG，供封面落 Asset 使用
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


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


def _make_stock_image(app, category_id, minio_key="img1.png", filename="img1.png"):
    from server.app.modules.image_library.models import StockImage

    with app.session_factory() as db:
        img = StockImage(category_id=category_id, minio_key=minio_key, filename=filename)
        db.add(img)
        db.commit()
        db.refresh(img)
        return img.id


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
            max_images=None,
            min_spacing=None,
            builtin_variant="conservative",
            format_model_selected=None,
            out_diagnostics=None,
        ):
            captured["article_id"] = article_id
            captured["candidates"] = candidate_categories
            captured["include_images"] = include_images
            captured["web_fallback"] = web_fallback
            captured["preset_id"] = preset_id
            captured["max_images"] = max_images
            captured["min_spacing"] = min_spacing
            captured["builtin_variant"] = builtin_variant

        monkeypatch.setattr("server.app.modules.articles.ai_illustrate_svc.run_ai_format", _stub)

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
        # 默认激进：用「积极配图」变体，数量旋钮默认 12 / 1，无自定义 preset
        assert captured["builtin_variant"] == "aggressive"
        assert captured["max_images"] == 12
        assert captured["min_spacing"] == 1
        assert captured["preset_id"] is None
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
            "server.app.modules.articles.ai_illustrate_svc.run_ai_format",
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

        monkeypatch.setattr("server.app.modules.articles.ai_illustrate_svc.run_ai_format", _stub)

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
def test_ai_illustrate_sets_cover_by_default(monkeypatch):
    """set_cover 默认开：主推栏目有图时给无封面文章配封面，covers_set 计数回传。"""
    app = build_test_app(monkeypatch)
    try:
        main_id = _make_category(app, "主推A", "main-a", "main")
        _make_stock_image(app, main_id)
        aid = _make_article(app.client)
        uid = _uid(app)

        monkeypatch.setattr(
            "server.app.modules.articles.ai_illustrate_svc.run_ai_format",
            lambda article_id, **kw: 1,
        )
        monkeypatch.setattr(
            "server.app.modules.image_library.store.get_object_bytes",
            lambda bucket, key: _PNG_1x1,
        )

        from server.app.modules.articles.models import Article
        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        res = run_ai_illustrate(
            NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={"main_category_id": main_id},  # 不传 set_cover → 默认开
                inputs={"article_ids": [aid]},
                upstream={},
            )
        )
        assert res.output["covers_set"] == 1
        assert res.output["cover_errors"] == []
        with app.session_factory() as db:
            assert db.get(Article, aid).cover_asset_id is not None
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_ai_illustrate_set_cover_off_skips_cover(monkeypatch):
    """set_cover=False：完全不碰封面，也不触发 MinIO 取图。"""
    app = build_test_app(monkeypatch)
    try:
        main_id = _make_category(app, "主推A", "main-a", "main")
        _make_stock_image(app, main_id)
        aid = _make_article(app.client)
        uid = _uid(app)

        monkeypatch.setattr(
            "server.app.modules.articles.ai_illustrate_svc.run_ai_format",
            lambda article_id, **kw: 1,
        )

        def _must_not_fetch(bucket, key):
            raise AssertionError("set_cover=False 不应取图")

        monkeypatch.setattr(
            "server.app.modules.image_library.store.get_object_bytes", _must_not_fetch
        )

        from server.app.modules.articles.models import Article
        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        res = run_ai_illustrate(
            NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={"main_category_id": main_id, "set_cover": False},
                inputs={"article_ids": [aid]},
                upstream={},
            )
        )
        assert res.output["covers_set"] == 0
        with app.session_factory() as db:
            assert db.get(Article, aid).cover_asset_id is None
    finally:
        app.cleanup()


def _capture_knobs(monkeypatch):
    """把节点传给 run_ai_format 的关键 kwargs 抓出来，供风格/数量旋钮断言。"""
    captured: dict = {}
    monkeypatch.setattr(
        "server.app.modules.articles.ai_illustrate_svc.run_ai_format",
        lambda article_id, **kw: captured.update(kw) or 0,
    )
    return captured


@pytest.mark.mysql
def test_ai_illustrate_conservative_toggle_off(monkeypatch):
    """aggressive_images=False → 保守变体 + 保守默认数量(3/5)。"""
    app = build_test_app(monkeypatch)
    try:
        main_id = _make_category(app, "主推A", "main-a", "main")
        aid = _make_article(app.client)
        uid = _uid(app)
        captured = _capture_knobs(monkeypatch)

        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        run_ai_illustrate(
            NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={
                    "main_category_id": main_id,
                    "aggressive_images": False,
                    "set_cover": False,
                },
                inputs={"article_ids": [aid]},
                upstream={},
            )
        )
        assert captured["builtin_variant"] == "conservative"
        assert captured["max_images"] == 3
        assert captured["min_spacing"] == 5
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_ai_illustrate_explicit_numbers_override_defaults(monkeypatch):
    """显式 max_images/min_spacing 覆盖风格默认；清空字段得到的 0 当作未设、回退默认。"""
    app = build_test_app(monkeypatch)
    try:
        main_id = _make_category(app, "主推A", "main-a", "main")
        aid = _make_article(app.client)
        uid = _uid(app)
        captured = _capture_knobs(monkeypatch)

        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        run_ai_illustrate(
            NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={
                    "main_category_id": main_id,
                    "max_images": 6,
                    "min_spacing": 0,  # 前端清空 → Number("")==0 → 回退激进默认 1
                    "set_cover": False,
                },
                inputs={"article_ids": [aid]},
                upstream={},
            )
        )
        assert captured["max_images"] == 6  # 显式值生效
        assert captured["min_spacing"] == 1  # 0 视为未设，回退激进默认
        assert captured["builtin_variant"] == "aggressive"
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_ai_illustrate_custom_preset_passthrough(monkeypatch):
    """配了自定义 ai_format 模板 preset_id → 透传给 run_ai_format（变体仍按风格传，缺省兜底用）。"""
    app = build_test_app(monkeypatch)
    try:
        main_id = _make_category(app, "主推A", "main-a", "main")
        aid = _make_article(app.client)
        uid = _uid(app)
        captured = _capture_knobs(monkeypatch)

        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        run_ai_illustrate(
            NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={"main_category_id": main_id, "preset_id": 99, "set_cover": False},
                inputs={"article_ids": [aid]},
                upstream={},
            )
        )
        assert captured["preset_id"] == 99
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


@pytest.mark.mysql
def test_illustrate_one_surfaces_partial_miss(monkeypatch):
    """illustrate_one 端到端：run_ai_format 经 out_diagnostics 报 requested=2/inserted=1（部分 miss）时，
    IllustrateResult 须带 partial warning + requested/missed 字段，而非 inserted 非 0 就静默当成功。"""
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles import ai_illustrate_svc

        main_id = _make_category(app, "主推A", "main-a", "main")
        aid = _make_article(app.client)
        uid = _uid(app)

        def fake_run(article_id, *, out_diagnostics=None, **kw):
            # 模拟 run_ai_format：透传 diag（应配 2 张、只来 1 张，游戏乙没补到）+ 返回 1
            if out_diagnostics is not None:
                out_diagnostics.update(
                    {"requested": 2, "inserted": 1, "missed": 1, "missed_games": ["游戏乙"]}
                )
            return 1

        monkeypatch.setattr(ai_illustrate_svc, "run_ai_format", fake_run)

        result = ai_illustrate_svc.illustrate_one(
            article_id=aid,
            main_category_id=main_id,
            user_id=uid,
            options=ai_illustrate_svc.IllustrateOptions(set_cover=False, web_fallback=True),
            session_factory=app.session_factory,
        )
        assert result.images_inserted == 1
        assert result.requested == 2 and result.missed == 1
        assert result.format_error is None
        assert result.warning and "游戏乙" in result.warning
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_ai_format_prepare_threads_selected_model(monkeypatch):
    """_ai_format_prepare 把 format_model_selected 透传给 resolve_format_engine，
    并把解析出的 model/base_url（含 Claude 中转地址）带进 prep。"""
    app = build_test_app(monkeypatch)
    try:
        aid = _make_article(app.client)
        uid = _uid(app)
        captured: dict = {}

        def _spy_resolve(db, selected=None):
            captured["selected"] = selected
            return ("relay-model-x", "sk-relay", "https://relay.example/v1", 120)

        # resolve_format_engine 在 _ai_format_prepare 内部按需 import，故 patch 源模块属性
        monkeypatch.setattr(
            "server.app.modules.ai_models.service.resolve_format_engine", _spy_resolve
        )

        from server.app.modules.articles.ai_format import _ai_format_prepare

        prep = _ai_format_prepare(
            aid,
            lock_started_at=None,
            include_images=False,
            preset_id=None,
            user_id=uid,
            candidate_categories=None,
            max_images=None,
            min_spacing=None,
            builtin_variant="conservative",
            format_model_selected="relay-model-x",
        )
        assert captured["selected"] == "relay-model-x"
        assert prep is not None
        assert prep.model == "relay-model-x"
        assert prep.base_url == "https://relay.example/v1"
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_ai_illustrate_passes_format_model(monkeypatch):
    """节点 cfg.format_engine → IllustrateOptions.format_model → run_ai_format(format_model_selected=...)。"""
    app = build_test_app(monkeypatch)
    try:
        main_id = _make_category(app, "主推A", "main-a", "main")
        aid = _make_article(app.client)
        uid = _uid(app)
        captured = _capture_knobs(monkeypatch)

        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        run_ai_illustrate(
            NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={
                    "main_category_id": main_id,
                    "format_engine": "relay-model-x",
                    "set_cover": False,
                },
                inputs={"article_ids": [aid]},
                upstream={},
            )
        )
        assert captured["format_model_selected"] == "relay-model-x"
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_ai_illustrate_format_model_default_none(monkeypatch):
    """不配 format_engine → format_model_selected 透传 None（走默认格式模型）。"""
    app = build_test_app(monkeypatch)
    try:
        main_id = _make_category(app, "主推A", "main-a", "main")
        aid = _make_article(app.client)
        uid = _uid(app)
        captured = _capture_knobs(monkeypatch)

        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        run_ai_illustrate(
            NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={"main_category_id": main_id, "set_cover": False},
                inputs={"article_ids": [aid]},
                upstream={},
            )
        )
        assert captured["format_model_selected"] is None
    finally:
        app.cleanup()
