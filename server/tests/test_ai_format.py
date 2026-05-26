"""Tests for AI format lock handling and正文小标题 conversion."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from server.tests.utils import build_test_app


def _create_article(client, content_json: dict | None = None) -> dict:
    response = client.post(
        "/api/articles",
        json={
            "title": "AI format test article",
            "content_json": content_json
            or {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}]},
        },
    )
    assert response.status_code == 200
    return response.json()


def _fake_completion(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _wait_until_unlocked(test_app, article_id: int, timeout: float = 3.0) -> None:
    from server.app.modules.articles.models import Article

    deadline = time.time() + timeout
    while time.time() < deadline:
        with test_app.session_factory() as db:
            article = db.get(Article, article_id)
            if article is not None and not article.ai_checking:
                return
        time.sleep(0.05)
    raise AssertionError("article stayed ai_checking=True")


@pytest.mark.mysql
def test_edit_locked_article_returns_409(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        from server.app.modules.articles.models import Article

        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            db_article.ai_checking = True
            db_article.ai_checking_started_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()

        response = client.put(
            f"/api/articles/{article_id}",
            json={"title": "changed title"},
        )
        assert response.status_code == 409
        assert "AI" in response.json()["detail"]
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_delete_locked_article_returns_409(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        from server.app.modules.articles.models import Article

        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            db_article.ai_checking = True
            db_article.ai_checking_started_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()

        response = client.delete(f"/api/articles/{article_id}")
        assert response.status_code == 409
        assert "AI" in response.json()["detail"]
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_edit_expired_lock_allows_update(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        from server.app.modules.articles.models import Article

        expired_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=121)

        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            db_article.ai_checking = True
            db_article.ai_checking_started_at = expired_time
            db.commit()

        response = client.put(
            f"/api/articles/{article_id}",
            json={"title": "expired lock update"},
        )
        assert response.status_code == 200
        assert response.json()["title"] == "expired lock update"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_read_expired_lock_clears_ai_checking(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        from server.app.modules.articles.models import Article

        expired_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=121)
        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            db_article.ai_checking = True
            db_article.ai_checking_started_at = expired_time
            db.commit()

        response = client.get(f"/api/articles/{article_id}")
        assert response.status_code == 200
        assert response.json()["ai_checking"] is False

        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            assert db_article.ai_checking is False
            assert db_article.ai_checking_started_at is None
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_edit_unlocked_article_succeeds(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(client)
        article_id = article["id"]

        response = client.put(
            f"/api/articles/{article_id}",
            json={"title": "normal update"},
        )
        assert response.status_code == 200
        assert response.json()["title"] == "normal update"
    finally:
        test_app.cleanup()


from server.app.modules.articles.ai_format import (
    _apply_headings,
    _derive_html_and_text,
    _node_text,
    _top_level_text_nodes,
)


def test_top_level_text_nodes_returns_paragraphs_and_headings():
    doc = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 1}, "content": []},
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]},
            {"type": "image", "attrs": {"src": "/x.png"}},
        ],
    }
    result = _top_level_text_nodes(doc)
    assert [item[0] for item in result] == [0, 1]


def test_node_text_joins_text_and_hard_break_nodes():
    node = {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "Hello"},
            {"type": "hardBreak"},
            {"type": "text", "text": "World"},
        ],
    }
    assert _node_text(node) == "Hello\nWorld"


def test_apply_headings_converts_paragraph_to_h1():
    doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Title"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Body"}]},
        ],
    }
    result = _apply_headings(doc, heading_indices={0})
    assert result["content"][0]["type"] == "heading"
    assert result["content"][0]["attrs"]["level"] == 1
    assert result["content"][1]["type"] == "paragraph"


def test_apply_headings_downgrades_unselected_heading():
    doc = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 1}, "content": [{"type": "text", "text": "Too long sentence."}]},
        ],
    }
    result = _apply_headings(doc, heading_indices=set())
    assert result["content"][0]["type"] == "paragraph"
    assert "attrs" not in result["content"][0]


def test_derive_html_and_text_generates_correct_output():
    doc = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 1}, "content": [{"type": "text", "text": "Title"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Body"}]},
        ],
    }
    html, text = _derive_html_and_text(doc)
    assert html == "<h1>Title</h1><p>Body</p>"
    assert "Title" in text
    assert "Body" in text


@pytest.mark.mysql
def test_ai_format_empty_indices_releases_lock_without_changing_content(monkeypatch):
    test_app = build_test_app(monkeypatch)

    try:
        article = _create_article(test_app.client)
        article_id = article["id"]

        from server.app.modules.articles.models import Article
        from server.app.modules.articles.ai_format import run_ai_format

        lock_started_at = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            original_content = db_article.content_json
            db_article.ai_checking = True
            db_article.ai_checking_started_at = lock_started_at
            db.commit()

        monkeypatch.setattr(
            "server.app.modules.articles.ai_format._call_litellm_completion",
            lambda **_: _fake_completion('{"heading_indices": []}'),
        )

        run_ai_format(article_id, include_images=False, lock_started_at=lock_started_at)

        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            assert db_article.ai_checking is False
            assert db_article.ai_checking_started_at is None
            assert db_article.content_json == original_content
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_ai_format_button_path_does_not_trigger_image_insertion(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article = _create_article(test_app.client)
        article_id = article["id"]

        from server.app.modules.articles.models import Article
        from server.app.modules.image_library.models import StockCategory

        with test_app.session_factory() as db:
            category = StockCategory(name="covers", bucket_name="covers")
            db.add(category)
            db.flush()
            db_article = db.get(Article, article_id)
            db_article.stock_categories = [category]  # 多对多关联
            db.commit()

        image_insert_called = False

        def fake_maybe_insert_images(*args, **kwargs):
            nonlocal image_insert_called
            image_insert_called = True
            return args[0], 0

        monkeypatch.setattr(
            "server.app.modules.articles.ai_format._call_litellm_completion",
            lambda **_: _fake_completion('{"heading_indices": [0], "image_positions": [{"index": 0, "hint": "风景描写"}]}'),
        )
        monkeypatch.setattr("server.app.modules.articles.ai_format._maybe_insert_images", fake_maybe_insert_images)

        response = client.post(f"/api/articles/{article_id}/ai-format")
        assert response.status_code == 202
        _wait_until_unlocked(test_app, article_id)

        assert image_insert_called is False
        with test_app.session_factory() as db:
            db_article = db.get(Article, article_id)
            assert "image" not in db_article.content_json
    finally:
        test_app.cleanup()


# ── _maybe_insert_images 单元测试（不依赖数据库）────────────────────────────

def _make_article_stub(stock_category_id=None, stock_categories=None):
    """构造 Article stub，模拟 ORM 对象的关键属性。"""
    return SimpleNamespace(
        stock_category_id=stock_category_id,
        stock_categories=stock_categories or [],
    )


def _simple_content():
    """最小化的 Tiptap doc，包含两个段落节点（索引 0 和 1）。"""
    return {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "段落一"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "段落二"}]},
        ],
    }


def test_maybe_insert_images_skips_when_no_stock_categories(monkeypatch):
    """stock_categories 为空且 stock_category_id 为 None → 不插图。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    select_called = []
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.select_images_by_hints",
        lambda *a, **kw: select_called.append(1) or [],
    )

    content = _simple_content()
    parsed = {"image_positions": [{"index": 0, "hint": "风景"}]}
    article = _make_article_stub()
    result_json, count = _maybe_insert_images(content, parsed, article, db=None)

    assert count == 0
    assert select_called == []  # 选图函数未被调用


def test_maybe_insert_images_skips_when_hint_is_none_or_empty(monkeypatch):
    """hint 为 None 或空字符串 → 不插图，select_images_by_hints 不被调用。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    select_called = []
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.select_images_by_hints",
        lambda *a, **kw: select_called.append(1) or [None, None],
    )
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.fetch_image_by_id",
        lambda *a, **kw: None,
    )

    cat = SimpleNamespace(id=1)
    article = _make_article_stub(stock_categories=[cat])

    # hint=None
    parsed = {"image_positions": [{"index": 0, "hint": None}]}
    _, count = _maybe_insert_images(_simple_content(), parsed, article, db=None)
    assert count == 0

    # hint="" (空字符串)
    parsed2 = {"image_positions": [{"index": 0, "hint": ""}]}
    _, count2 = _maybe_insert_images(_simple_content(), parsed2, article, db=None)
    assert count2 == 0


def test_maybe_insert_images_skips_when_no_library_match(monkeypatch):
    """hint 有值但图库无匹配（返回 None）→ 不插图，不降级随机。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.select_images_by_hints",
        lambda category_ids, hints, db: [None] * len(hints),
    )
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.fetch_image_by_id",
        lambda *a, **kw: None,
    )

    cat = SimpleNamespace(id=1)
    article = _make_article_stub(stock_categories=[cat])
    parsed = {"image_positions": [{"index": 0, "hint": "不存在的主题"}]}
    _, count = _maybe_insert_images(_simple_content(), parsed, article, db=None)

    assert count == 0


def test_maybe_insert_images_inserts_when_hint_matches(monkeypatch):
    """hint 匹配到图片 → 插入图片，count=1。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    fake_ref = SimpleNamespace(id=42, url="/api/stock-images/42/file", filename="test.jpg", width=800, height=600)

    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.select_images_by_hints",
        lambda category_ids, hints, db: [42],
    )
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.fetch_image_by_id",
        lambda image_id, db: fake_ref,
    )
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.has_images_in_content",
        lambda content: False,
    )

    # insert_images_at_positions 只需验证调用了，返回原 content 即可
    inserted_positions = []
    def fake_insert(content_json, refs, positions):
        inserted_positions.extend(positions)
        return content_json

    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.insert_images_at_positions",
        fake_insert,
    )

    cat = SimpleNamespace(id=1)
    article = _make_article_stub(stock_categories=[cat])
    parsed = {"image_positions": [{"index": 0, "hint": "风景"}]}
    _, count = _maybe_insert_images(_simple_content(), parsed, article, db=None)

    assert count == 1
    assert 0 in inserted_positions


def test_maybe_insert_images_uses_all_category_ids(monkeypatch):
    """select_images_by_hints 收到的 category_ids 包含 stock_categories 中的所有 ID。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    received_ids = []

    def fake_select(category_ids, hints, db):
        received_ids.extend(category_ids)
        return [None] * len(hints)

    monkeypatch.setattr("server.app.modules.articles.ai_format.select_images_by_hints", fake_select)
    monkeypatch.setattr("server.app.modules.articles.ai_format.fetch_image_by_id", lambda *a, **kw: None)

    cats = [SimpleNamespace(id=10), SimpleNamespace(id=20), SimpleNamespace(id=30)]
    article = _make_article_stub(stock_categories=cats)
    parsed = {"image_positions": [{"index": 0, "hint": "主题"}]}
    _maybe_insert_images(_simple_content(), parsed, article, db=None)

    assert set(received_ids) == {10, 20, 30}


def test_maybe_insert_images_old_format_integers_skipped(monkeypatch):
    """旧格式（纯整数数组）→ hint 全为 None → 全部跳过，不插图。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    select_called = []
    monkeypatch.setattr(
        "server.app.modules.articles.ai_format.select_images_by_hints",
        lambda category_ids, hints, db: select_called.append(hints) or [None] * len(hints),
    )
    monkeypatch.setattr("server.app.modules.articles.ai_format.fetch_image_by_id", lambda *a, **kw: None)

    cat = SimpleNamespace(id=1)
    article = _make_article_stub(stock_categories=[cat])
    parsed = {"image_positions": [0, 1]}  # 旧格式：纯整数
    _, count = _maybe_insert_images(_simple_content(), parsed, article, db=None)

    assert count == 0
    # select 被调用，但 hints 全是 None，内部会全部跳过
    if select_called:
        assert all(h is None for h in select_called[0])


def test_maybe_insert_images_fallback_to_old_stock_category_id(monkeypatch):
    """stock_categories 为空但 stock_category_id 有值 → 兼容旧字段，尝试选图。"""
    from server.app.modules.articles.ai_format import _maybe_insert_images

    received_ids = []

    def fake_select(category_ids, hints, db):
        received_ids.extend(category_ids)
        return [None] * len(hints)

    monkeypatch.setattr("server.app.modules.articles.ai_format.select_images_by_hints", fake_select)
    monkeypatch.setattr("server.app.modules.articles.ai_format.fetch_image_by_id", lambda *a, **kw: None)

    # stock_categories 为空，只有旧字段
    article = _make_article_stub(stock_category_id=99, stock_categories=[])
    parsed = {"image_positions": [{"index": 0, "hint": "风景"}]}
    _maybe_insert_images(_simple_content(), parsed, article, db=None)

    # 应当把旧的 stock_category_id 包含进 category_ids
    assert 99 in received_ids
