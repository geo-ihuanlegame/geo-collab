"""配图兜底：检查 + 随机补图。Task 1 纯函数单测（无需 MySQL，stub selector）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.app.modules.image_library import fallback as fb
from server.app.modules.image_library.selector import StockImageRef


def _doc(*nodes):
    return {"type": "doc", "content": list(nodes)}


def _para(text):
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def _img(stock_id):
    return {"type": "image", "attrs": {"src": "/x", "stockImageId": stock_id}}


def _ref(image_id):
    return StockImageRef(
        id=image_id, url=f"/u/{image_id}", filename=f"{image_id}.jpg", width=800, height=400
    )


def _seq_pick(pool):
    """返回一个 pick_image_id 替身：从 pool 里给出第一个不在 excluded_ids 的 id。"""

    def _pick(query, db):
        for pid in pool:
            if pid not in query.excluded_ids:
                return pid
        return None

    return _pick


def test_count_body_images():
    assert fb.count_body_images(_doc(_para("a"))) == 0
    assert fb.count_body_images(_doc(_para("a"), _img(1), _img(2))) == 2


def test_collect_used_stock_image_ids():
    assert fb.collect_used_stock_image_ids(_doc(_para("a"), _img(7), _img(9))) == {7, 9}


def test_fill_random_images_inserts_gap(monkeypatch):
    monkeypatch.setattr(fb, "pick_image_id", _seq_pick([101, 102, 103]))
    monkeypatch.setattr(fb, "fetch_image_by_id", lambda i, db: _ref(i))
    article = SimpleNamespace(content_json=_doc(_para("a"), _para("b"), _para("c")), version=1)
    db = SimpleNamespace(commit=lambda: None)
    n = fb.fill_random_images(db, article, category_ids=[5], gap=2)
    assert n == 2
    assert fb.count_body_images(article.content_json) == 2
    assert article.version == 2


def test_fill_random_images_dedups_used(monkeypatch):
    # 正文已含 101；候选只有 101 → 取不到新图 → 0，且不抛异常
    monkeypatch.setattr(fb, "pick_image_id", _seq_pick([101]))
    monkeypatch.setattr(fb, "fetch_image_by_id", lambda i, db: _ref(i))
    article = SimpleNamespace(content_json=_doc(_para("a"), _img(101)), version=1)
    db = SimpleNamespace(commit=lambda: None)
    assert fb.fill_random_images(db, article, category_ids=[5], gap=1) == 0


def test_apply_fallback_fills_to_target(monkeypatch):
    # anchored=3, current=1 → target=3 → 补 2（部分失败：锚定 3 个、已配 1 个，补 2）
    monkeypatch.setattr(fb, "pick_image_id", _seq_pick([201, 202, 203]))
    monkeypatch.setattr(fb, "fetch_image_by_id", lambda i, db: _ref(i))
    article = SimpleNamespace(
        content_json=_doc(_para("a"), _img(9), _para("b"), _para("c")),
        is_deleted=False,
        version=1,
    )
    db = SimpleNamespace(get=lambda model, _id: article, commit=lambda: None, close=lambda: None)
    n = fb.apply_image_fallback(
        article_id=1, anchored=3, category_ids=[5], max_images=12, session_factory=lambda: db
    )
    assert n == 2
    assert fb.count_body_images(article.content_json) == 3


def test_apply_fallback_noop_when_enough():
    # anchored=2, current=3 → target=2, gap<0 → 0
    article = SimpleNamespace(
        content_json=_doc(_img(1), _img(2), _img(3)), is_deleted=False, version=1
    )
    db = SimpleNamespace(get=lambda m, i: article, commit=lambda: None, close=lambda: None)
    n = fb.apply_image_fallback(
        article_id=1, anchored=2, category_ids=[5], max_images=12, session_factory=lambda: db
    )
    assert n == 0


def test_apply_fallback_noop_when_no_categories():
    assert (
        fb.apply_image_fallback(
            article_id=1, anchored=3, category_ids=[], max_images=12, session_factory=lambda: None
        )
        == 0
    )


def test_apply_fallback_no_flood_when_anchored_zero(monkeypatch):
    # #1182 回归：锚定全失败 anchored=0 → 绝不补图，即便图库有图、正文很长、作者意图很大。
    # 旧实现 target=min(max(requested,1),max_images)：当 illustrate_one 传 requested=10（清单长度）
    # 时会灌满 10 张随机无关图。新实现用 anchored（实际锚定数）=0 → 一张都不补。
    monkeypatch.setattr(fb, "pick_image_id", _seq_pick([401, 402, 403, 404, 405]))
    monkeypatch.setattr(fb, "fetch_image_by_id", lambda i, db: _ref(i))
    article = SimpleNamespace(
        content_json=_doc(*[_para(f"p{i}") for i in range(10)]),
        is_deleted=False,
        version=1,
    )
    db = SimpleNamespace(get=lambda m, i: article, commit=lambda: None, close=lambda: None)
    n = fb.apply_image_fallback(
        article_id=1, anchored=0, category_ids=[5], max_images=12, session_factory=lambda: db
    )
    assert n == 0
    assert fb.count_body_images(article.content_json) == 0
    assert article.version == 1  # 文档没被动过


def test_apply_fallback_capped_by_max_images(monkeypatch):
    # anchored=10 但 max_images=3、current=0 → 只补 3（硬上限封顶，绝不超）
    monkeypatch.setattr(fb, "pick_image_id", _seq_pick([501, 502, 503, 504, 505]))
    monkeypatch.setattr(fb, "fetch_image_by_id", lambda i, db: _ref(i))
    article = SimpleNamespace(
        content_json=_doc(*[_para(f"p{i}") for i in range(8)]),
        is_deleted=False,
        version=1,
    )
    db = SimpleNamespace(get=lambda m, i: article, commit=lambda: None, close=lambda: None)
    n = fb.apply_image_fallback(
        article_id=1, anchored=10, category_ids=[5], max_images=3, session_factory=lambda: db
    )
    assert n == 3
    assert fb.count_body_images(article.content_json) == 3


def test_fill_random_images_empty_body_returns_zero(monkeypatch):
    # 空正文：均匀位为空 → 不插、不 bump version、返回 0
    monkeypatch.setattr(fb, "pick_image_id", _seq_pick([301, 302]))
    monkeypatch.setattr(fb, "fetch_image_by_id", lambda i, db: _ref(i))
    article = SimpleNamespace(content_json={"type": "doc", "content": []}, version=1)
    db = SimpleNamespace(commit=lambda: None)
    assert fb.fill_random_images(db, article, category_ids=[5], gap=2) == 0
    assert article.version == 1
    assert fb.count_body_images(article.content_json) == 0


def test_spread_positions_skips_adjacent_images_and_spreads():
    doc = _doc(_para("a"), _img(1), _para("b"), _para("c"), _para("d"))
    # n=1：候选是没有紧邻 image 的块下标；返回 1 个、落在候选内
    pos = fb._spread_positions(doc, 1)
    assert len(pos) == 1
    # 下标 0 的 para 后面紧跟 image(下标1) → 被跳过；候选应为 {2,3,4}
    assert pos[0] in {2, 3, 4}
    # 空文档 → []
    assert fb._spread_positions(_doc(), 2) == []


@pytest.mark.mysql
def test_illustrate_one_fills_missed_via_endpoint(monkeypatch):
    """run_ai_format inserts 1 image but requested=3, so fallback fills 2 more."""
    from server.app.modules.articles.models import Article
    from server.app.modules.articles.parser import dumps_content_json, loads_content_json
    from server.app.modules.image_library.models import StockCategory, StockImage
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        with test_app.session_factory() as db:
            cat = StockCategory(name="main-test", bucket_name="b-main-test", kind="main")
            db.add(cat)
            db.flush()
            for i in range(3):
                db.add(
                    StockImage(
                        category_id=cat.id,
                        minio_key=f"k-fallback-{i}",
                        filename=f"img{i}.jpg",
                        width=800,
                        height=400,
                    )
                )

            article = Article(
                user_id=test_app.admin_id,
                title="top games",
                content_json=dumps_content_json(
                    {
                        "type": "doc",
                        "content": [
                            {
                                "type": "heading",
                                "attrs": {"level": 2},
                                "content": [{"type": "text", "text": "Game A"}],
                            },
                            {"type": "paragraph", "content": [{"type": "text", "text": "Body 1"}]},
                            {"type": "paragraph", "content": [{"type": "text", "text": "Body 2"}]},
                        ],
                    }
                ),
                content_html="<h2>Game A</h2><p>Body 1</p><p>Body 2</p>",
                plain_text="Game A Body 1 Body 2",
                version=1,
            )
            db.add(article)
            db.commit()
            cat_id = cat.id
            article_id = article.id

        def fake_run_ai_format(article_id_, **kwargs):
            with test_app.session_factory() as db:
                art = db.get(Article, article_id_)
                content = loads_content_json(art.content_json)
                nodes = list(content.get("content") or [])
                nodes.insert(1, {"type": "image", "attrs": {"src": "/x", "stockImageId": None}})
                art.content_json = dumps_content_json({**content, "content": nodes})
                art.version = (art.version or 0) + 1
                db.commit()
            out = kwargs.get("out_diagnostics")
            if out is not None:
                out.update(
                    {
                        "requested": 3,
                        "anchored": 3,
                        "inserted": 1,
                        "missed": 2,
                        "missed_games": ["B", "C"],
                    }
                )
            return 1

        monkeypatch.setattr(
            "server.app.modules.articles.ai_illustrate_svc.run_ai_format",
            fake_run_ai_format,
        )

        r = test_app.client.post(
            f"/api/articles/{article_id}/ai-illustrate",
            json={"main_category_id": cat_id, "set_cover": False},
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["fallback_inserted"] == 2
        assert body["images_inserted"] == 3

        with test_app.session_factory() as db:
            art = db.get(Article, article_id)
            content = loads_content_json(art.content_json)
            img_count = sum(
                1
                for node in content["content"]
                if isinstance(node, dict) and node.get("type") == "image"
            )
            assert img_count == 3
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_illustrate_one_no_flood_when_anchored_zero(monkeypatch):
    """#1182 端到端回归：锚定全失败（requested=10 但 anchored=0、inserted=0）时，
    即便图库有图，兜底也必须补 0 张，绝不灌满随机无关图。"""
    from server.app.modules.articles.models import Article
    from server.app.modules.articles.parser import dumps_content_json, loads_content_json
    from server.app.modules.image_library.models import StockCategory, StockImage
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        with test_app.session_factory() as db:
            cat = StockCategory(name="main-noflood", bucket_name="b-noflood", kind="main")
            db.add(cat)
            db.flush()
            # 图库【有图】——证明兜底"能"灌但"不"灌
            for i in range(5):
                db.add(
                    StockImage(
                        category_id=cat.id,
                        minio_key=f"k-noflood-{i}",
                        filename=f"nf{i}.jpg",
                        width=800,
                        height=400,
                    )
                )
            article = Article(
                user_id=test_app.admin_id,
                title="10 games as paragraphs",
                content_json=dumps_content_json(
                    {
                        "type": "doc",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": f"游戏{i}"}]}
                            for i in range(10)
                        ],
                    }
                ),
                content_html="<p>x</p>",
                plain_text="x",
                version=1,
            )
            db.add(article)
            db.commit()
            cat_id = cat.id
            article_id = article.id

        def fake_run_ai_format(article_id_, **kwargs):
            # 锚定全失败：不插任何图，诊断 requested=10（清单长度）但 anchored=0、inserted=0
            out = kwargs.get("out_diagnostics")
            if out is not None:
                out.update(
                    {
                        "requested": 10,
                        "anchored": 0,
                        "inserted": 0,
                        "skip_reason": "ai_returned_no_positions",
                    }
                )
            return 0

        monkeypatch.setattr(
            "server.app.modules.articles.ai_illustrate_svc.run_ai_format",
            fake_run_ai_format,
        )

        r = test_app.client.post(
            f"/api/articles/{article_id}/ai-illustrate",
            json={"main_category_id": cat_id, "set_cover": False},
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["fallback_inserted"] == 0
        assert body["images_inserted"] == 0

        with test_app.session_factory() as db:
            art = db.get(Article, article_id)
            content = loads_content_json(art.content_json)
            img_count = sum(
                1
                for node in content["content"]
                if isinstance(node, dict) and node.get("type") == "image"
            )
            assert img_count == 0
    finally:
        test_app.cleanup()


def test_ai_illustrate_node_aggregates_fallback(monkeypatch):
    """Node output sums fallback_inserted across articles."""
    from server.app.modules.articles.ai_illustrate_svc import IllustrateResult
    from server.app.modules.pipelines.nodes import ai_illustrate as node_mod
    from server.app.modules.pipelines.nodes.base import NodeRunContext

    returns = {
        1: IllustrateResult(
            article_id=1, images_inserted=3, fallback_inserted=2, cover_status="skipped"
        ),
        2: IllustrateResult(
            article_id=2, images_inserted=2, fallback_inserted=1, cover_status="skipped"
        ),
    }
    monkeypatch.setattr(node_mod, "illustrate_one", lambda *, article_id, **kw: returns[article_id])

    ctx = NodeRunContext(
        session_factory=lambda: None,
        user_id=1,
        config={"main_category_id": 5},
        inputs={"article_ids": [1, 2]},
        upstream={},
    )
    result = node_mod.run_ai_illustrate(ctx)
    assert result.output["fallback_inserted"] == 3
    assert result.output["images_inserted"] == 5
