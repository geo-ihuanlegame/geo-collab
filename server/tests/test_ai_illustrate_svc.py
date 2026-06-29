"""ai_illustrate_svc 单篇文章配图 + 自动封面 service 测试。

mock 掉 run_ai_format 和 set_random_cover_from_category —— 此处只测调度逻辑，
配图本身的正确性由 articles.ai_format 自己的测试覆盖。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.tests.utils import build_test_app


def _mk_article(test_app, *, title: str = "t", content_json: dict | None = None) -> int:
    """建一篇带 ai_format_targets 的最小 article。"""
    from server.app.modules.articles.models import Article

    doc = content_json or {
        "type": "doc",
        "content": [
            # 含 heading 节点 → has_ai_format_targets 返 True
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "标题"}],
            },
            {"type": "paragraph", "content": [{"type": "text", "text": "段落"}]},
        ],
    }
    db = test_app.session_factory()
    try:
        a = Article(
            user_id=test_app.admin_id,
            title=title,
            content_json=json.dumps(doc, ensure_ascii=False),
            content_html="",
            plain_text="标题 段落",
            word_count=4,
            status="draft",
            review_status="pending",
        )
        db.add(a)
        db.commit()
        return a.id
    finally:
        db.close()


def _patch_run_ai_format(monkeypatch, return_value: int = 3) -> list:
    """Mock run_ai_format → 返指定 images_inserted；记录调用参数。"""
    calls: list[dict[str, Any]] = []

    def fake(article_id, **kwargs):
        calls.append({"article_id": article_id, **kwargs})
        return return_value

    monkeypatch.setattr("server.app.modules.articles.ai_illustrate_svc.run_ai_format", fake)
    return calls


def _patch_cover(monkeypatch, status: str = "set", error: str | None = None) -> list:
    """Mock set_random_cover_from_category → 返指定 CoverResult；记录参数。"""
    from server.app.modules.image_library.cover import CoverResult

    calls: list[dict[str, Any]] = []

    def fake(db, article, category_id, user_id):
        calls.append({"category_id": category_id, "user_id": user_id})
        return CoverResult(status=status, error=error)

    monkeypatch.setattr(
        "server.app.modules.articles.ai_illustrate_svc.set_random_cover_from_category",
        fake,
    )
    return calls


def _mk_stock_category(
    test_app, *, cat_id: int, name: str = "餐厅养成记", kind: str = "main"
) -> int:
    """Helper: 建一个最小 StockCategory 让 category_contexts_for 能 return 非空."""
    from server.app.modules.image_library.models import StockCategory

    db = test_app.session_factory()
    try:
        cat = StockCategory(
            id=cat_id,
            name=name,
            kind=kind,
            bucket_name=f"bucket-{cat_id}",
            description=f"test category {cat_id}",
        )
        db.add(cat)
        db.commit()
        return cat.id
    finally:
        db.close()


@pytest.mark.mysql
def test_illustrate_one_happy_path_returns_images_and_set_cover(monkeypatch):
    """run_ai_format 返 3 + cover set → result.images_inserted=3, cover_status=set。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )

        aid = _mk_article(test_app)
        # 关键：seed main_category_id=42 才能让 category_contexts_for 返非空
        _mk_stock_category(test_app, cat_id=42)
        fmt_calls = _patch_run_ai_format(monkeypatch, return_value=3)
        cover_calls = _patch_cover(monkeypatch, status="set")

        result = illustrate_one(
            article_id=aid,
            main_category_id=42,
            user_id=test_app.admin_id,
            options=IllustrateOptions(),
            session_factory=test_app.session_factory,
        )

        assert result.article_id == aid
        assert result.images_inserted == 3
        assert result.cover_status == "set"
        assert result.cover_error is None
        assert result.format_error is None
        assert len(fmt_calls) == 1
        # category_contexts_for 应该返我们刚 seed 的 main category
        cats = fmt_calls[0]["candidate_categories"]
        assert isinstance(cats, list) and len(cats) >= 1, f"expected non-empty list, got {cats!r}"
        assert any(c.get("id") == 42 for c in cats), f"expected id=42 in {cats!r}"
        assert len(cover_calls) == 1
        assert cover_calls[0]["category_id"] == 42
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_illustrate_one_article_missing_returns_format_error(monkeypatch):
    """article_id 不存在 → format_error="article not found or deleted"。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )

        _patch_run_ai_format(monkeypatch)
        _patch_cover(monkeypatch)

        result = illustrate_one(
            article_id=999999,
            main_category_id=1,
            user_id=test_app.admin_id,
            options=IllustrateOptions(),
            session_factory=test_app.session_factory,
        )

        assert result.images_inserted == 0
        assert result.format_error == "article not found or deleted"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_illustrate_one_no_ai_format_targets_returns_format_error(monkeypatch):
    """正文无任何 paragraph/heading 文本节点 → has_ai_format_targets=False → format_error。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )

        # 空文档：has_ai_format_targets 返 False
        aid = _mk_article(
            test_app,
            content_json={"type": "doc", "content": []},
        )
        _patch_run_ai_format(monkeypatch)
        _patch_cover(monkeypatch)

        result = illustrate_one(
            article_id=aid,
            main_category_id=1,
            user_id=test_app.admin_id,
            options=IllustrateOptions(),
            session_factory=test_app.session_factory,
        )

        assert result.format_error == "no ai_format_targets in content"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_illustrate_one_cover_error_surfaces_in_result(monkeypatch):
    """cover 失败 → cover_status=error + cover_error 非空，不影响 images_inserted。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )

        aid = _mk_article(test_app)
        _patch_run_ai_format(monkeypatch, return_value=2)
        _patch_cover(monkeypatch, status="error", error="minio timeout")

        result = illustrate_one(
            article_id=aid,
            main_category_id=1,
            user_id=test_app.admin_id,
            options=IllustrateOptions(),
            session_factory=test_app.session_factory,
        )

        assert result.images_inserted == 2  # 配图阶段照常成功
        assert result.cover_status == "error"
        assert result.cover_error == "minio timeout"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_illustrate_one_set_cover_false_skips_cover_stage(monkeypatch):
    """options.set_cover=False → 不调 set_random_cover，cover_status=skipped。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )

        aid = _mk_article(test_app)
        _patch_run_ai_format(monkeypatch, return_value=1)
        cover_calls = _patch_cover(monkeypatch)

        result = illustrate_one(
            article_id=aid,
            main_category_id=1,
            user_id=test_app.admin_id,
            options=IllustrateOptions(set_cover=False),
            session_factory=test_app.session_factory,
        )

        assert result.cover_status == "skipped"
        assert result.cover_error is None
        assert len(cover_calls) == 0  # cover 函数没被调
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_illustrate_one_reads_back_article_ai_format_error(monkeypatch):
    """run_ai_format 把错误吞掉写到 article.ai_format_error → service 阶段 3 回读出来。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )
        from server.app.modules.articles.models import Article

        aid = _mk_article(test_app)

        # fake run_ai_format 返 0 同时往 article.ai_format_error 写一条
        def fake(article_id, **kwargs):
            db = test_app.session_factory()
            try:
                article = db.get(Article, article_id)
                article.ai_format_error = "LLM timeout after 60s"
                db.commit()
            finally:
                db.close()
            return 0

        monkeypatch.setattr("server.app.modules.articles.ai_illustrate_svc.run_ai_format", fake)
        _patch_cover(monkeypatch)

        result = illustrate_one(
            article_id=aid,
            main_category_id=1,
            user_id=test_app.admin_id,
            options=IllustrateOptions(),
            session_factory=test_app.session_factory,
        )

        assert result.images_inserted == 0
        assert result.format_error == "LLM timeout after 60s"
        # 没有 [illustration_skip] 前缀 → warning 应为 None（这是真 error）
        assert result.warning is None
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_illustrate_one_strips_illustration_skip_prefix_into_warning(monkeypatch):
    """ai_format 把 [illustration_skip] xxx 写到 ai_format_error 时,svc 阶段 3 剥前缀放进 warning,
    并把 format_error 置回 None——区分 "AI 决策为空" 与 "真的 error"."""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )
        from server.app.modules.articles.models import Article

        aid = _mk_article(test_app)

        # fake run_ai_format 返 0 同时按 ai_format 新行为往 ai_format_error 写带前缀的 skip_reason
        def fake(article_id, **kwargs):
            db = test_app.session_factory()
            try:
                article = db.get(Article, article_id)
                article.ai_format_error = "[illustration_skip] ai_returned_no_positions"
                db.commit()
            finally:
                db.close()
            return 0

        monkeypatch.setattr("server.app.modules.articles.ai_illustrate_svc.run_ai_format", fake)
        _patch_cover(monkeypatch)

        result = illustrate_one(
            article_id=aid,
            main_category_id=1,
            user_id=test_app.admin_id,
            options=IllustrateOptions(),
            session_factory=test_app.session_factory,
        )

        assert result.images_inserted == 0
        assert result.format_error is None  # 不是真 error
        assert result.warning == "ai_returned_no_positions"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_illustrate_one_zero_images_no_error_falls_back_to_unknown_warning(monkeypatch):
    """0 张图 + ai_format_error 也为 None 时,svc 兜底加一个 unknown reason warning,
    防止未来 ai_format 改回不写前缀时 writer 依旧拿得到明确信号."""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )

        aid = _mk_article(test_app)
        _patch_run_ai_format(monkeypatch, return_value=0)  # 不写 ai_format_error
        _patch_cover(monkeypatch)

        result = illustrate_one(
            article_id=aid,
            main_category_id=1,
            user_id=test_app.admin_id,
            options=IllustrateOptions(),
            session_factory=test_app.session_factory,
        )

        assert result.images_inserted == 0
        assert result.format_error is None
        assert result.warning is not None
        assert "unknown reason" in result.warning
    finally:
        test_app.cleanup()


def _patch_game_list_format(monkeypatch, return_value: int = 5) -> list:
    """Mock run_ai_format_from_game_list（确定性落图入口）→ 记录调用参数。"""
    calls: list[dict[str, Any]] = []

    def fake(article_id, **kwargs):
        calls.append({"article_id": article_id, **kwargs})
        return return_value

    monkeypatch.setattr(
        "server.app.modules.articles.ai_illustrate_svc.run_ai_format_from_game_list", fake
    )
    return calls


def _stamp_game_positions(test_app, article_id: int, game_positions: list[dict]) -> None:
    from server.app.modules.articles.models import Article

    db = test_app.session_factory()
    try:
        art = db.get(Article, article_id)
        art.metrics = {"game_positions": game_positions}
        db.commit()
    finally:
        db.close()


@pytest.mark.mysql
def test_illustrate_reads_stamp_when_option_none(monkeypatch):
    """options.game_list=None 但文章 metrics 带 game_positions → 走确定性 run_ai_format_from_game_list。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )

        aid = _mk_article(test_app)
        _mk_stock_category(test_app, cat_id=42)
        _stamp_game_positions(test_app, aid, [{"game": "原神"}])
        weak = _patch_run_ai_format(monkeypatch, return_value=3)
        det = _patch_game_list_format(monkeypatch, return_value=5)
        _patch_cover(monkeypatch)

        result = illustrate_one(
            article_id=aid,
            main_category_id=42,
            user_id=test_app.admin_id,
            options=IllustrateOptions(),  # game_list 默认 None
            session_factory=test_app.session_factory,
        )

        assert result.images_inserted == 5
        assert len(det) == 1
        assert det[0]["game_list"] == [{"game": "原神"}]
        assert weak == []  # 弱模型路径未被调用
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_explicit_game_list_wins_over_stamp(monkeypatch):
    """显式 options.game_list 优先于文章 stamp。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )

        aid = _mk_article(test_app)
        _mk_stock_category(test_app, cat_id=42)
        _stamp_game_positions(test_app, aid, [{"game": "来自stamp"}])
        _patch_run_ai_format(monkeypatch, return_value=3)
        det = _patch_game_list_format(monkeypatch, return_value=5)
        _patch_cover(monkeypatch)

        illustrate_one(
            article_id=aid,
            main_category_id=42,
            user_id=test_app.admin_id,
            options=IllustrateOptions(game_list=[{"game": "显式优先"}]),
            session_factory=test_app.session_factory,
        )

        assert len(det) == 1
        assert det[0]["game_list"] == [{"game": "显式优先"}]  # 不读 stamp
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_no_stamp_no_option_uses_run_ai_format(monkeypatch):
    """无 stamp 且 options.game_list=None → 回退现有 run_ai_format（现状不破）。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )

        aid = _mk_article(test_app)
        _mk_stock_category(test_app, cat_id=42)
        weak = _patch_run_ai_format(monkeypatch, return_value=3)
        det = _patch_game_list_format(monkeypatch, return_value=5)
        _patch_cover(monkeypatch)

        result = illustrate_one(
            article_id=aid,
            main_category_id=42,
            user_id=test_app.admin_id,
            options=IllustrateOptions(),
            session_factory=test_app.session_factory,
        )

        assert result.images_inserted == 3
        assert len(weak) == 1
        assert det == []  # 确定性路径未被调用
    finally:
        test_app.cleanup()
