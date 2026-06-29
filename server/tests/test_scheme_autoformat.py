"""方案生文后自动 AI 排版/配图：_auto_format_article 设锁 + 透传全部 bucket。"""

from __future__ import annotations

import pytest

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_auto_format_article_sets_lock_and_passes_all_buckets(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        from server.app.modules.ai_generation import scheme_executor
        from server.app.modules.articles.models import Article
        from server.app.modules.image_library.models import StockCategory
        from server.app.modules.system.models import User

        resp = client.post(
            "/api/articles",
            json={
                "title": "auto format",
                "content_json": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "原神是一款开放世界游戏。"}],
                        }
                    ],
                },
            },
        )
        assert resp.status_code == 200
        article_id = resp.json()["id"]

        with test_app.session_factory() as db:
            db.add(StockCategory(name="原神", bucket_name="ys", description=None))
            user = db.query(User).first()
            user_id = user.id
            user.ai_format_preset_id = 42
            db.commit()

        captured = {}

        def fake_run_ai_format(aid, **kwargs):
            captured["article_id"] = aid
            captured.update(kwargs)

        monkeypatch.setattr(scheme_executor, "run_ai_format", fake_run_ai_format)

        scheme_executor._auto_format_article(article_id, user_id, test_app.session_factory)

        assert captured["article_id"] == article_id
        assert captured["include_images"] is True
        assert captured["preset_id"] == 42
        assert captured["user_id"] == user_id
        assert {c["name"] for c in captured["candidate_categories"]} == {"原神"}
        assert captured["lock_started_at"] is not None

        with test_app.session_factory() as db:
            art = db.get(Article, article_id)
            assert art.ai_checking is True
            assert art.ai_checking_started_at == captured["lock_started_at"]
    finally:
        test_app.cleanup()


def _make_article(client) -> int:
    resp = client.post(
        "/api/articles",
        json={
            "title": "auto format route",
            "content_json": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "原神是一款开放世界游戏。"}],
                    }
                ],
            },
        },
    )
    assert resp.status_code == 200
    return resp.json()["id"]


@pytest.mark.mysql
def test_auto_format_routes_to_game_list_when_stamped(monkeypatch):
    """文章 metrics 带 game_positions → 走确定性 run_ai_format_from_game_list，不调弱模型路径。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation import scheme_executor
        from server.app.modules.articles.models import Article
        from server.app.modules.system.models import User

        article_id = _make_article(test_app.client)
        with test_app.session_factory() as db:
            user_id = db.query(User).first().id
            art = db.get(Article, article_id)
            art.metrics = {"game_positions": [{"game": "原神"}]}
            db.commit()

        weak: dict = {}
        det: dict = {}
        monkeypatch.setattr(
            scheme_executor, "run_ai_format", lambda aid, **kw: weak.update(aid=aid, **kw)
        )
        monkeypatch.setattr(
            scheme_executor,
            "run_ai_format_from_game_list",
            lambda aid, **kw: det.update(aid=aid, **kw),
        )

        scheme_executor._auto_format_article(article_id, user_id, test_app.session_factory)

        assert det.get("aid") == article_id
        assert det.get("game_list") == [{"game": "原神"}]
        assert det.get("builtin_variant") == "aggressive"
        assert det.get("max_images") == 12
        assert weak == {}  # 弱模型路径未被调用
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_auto_format_falls_back_when_no_stamp(monkeypatch):
    """文章无 game_positions → 回退现有 run_ai_format（现状不破）。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation import scheme_executor
        from server.app.modules.system.models import User

        article_id = _make_article(test_app.client)
        with test_app.session_factory() as db:
            user_id = db.query(User).first().id

        weak: dict = {}
        det: dict = {}
        monkeypatch.setattr(
            scheme_executor, "run_ai_format", lambda aid, **kw: weak.update(aid=aid, **kw)
        )
        monkeypatch.setattr(
            scheme_executor,
            "run_ai_format_from_game_list",
            lambda aid, **kw: det.update(aid=aid, **kw),
        )

        scheme_executor._auto_format_article(article_id, user_id, test_app.session_factory)

        assert weak.get("aid") == article_id
        assert weak.get("include_images") is True
        assert det == {}  # 确定性路径未被调用
    finally:
        test_app.cleanup()
