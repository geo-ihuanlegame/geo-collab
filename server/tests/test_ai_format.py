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
    from server.app.models.article import Article

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

        from server.app.models.article import Article

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

        from server.app.models.article import Article

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

        from server.app.models.article import Article

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

        from server.app.models.article import Article

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

        from server.app.models.article import Article
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

        from server.app.models import Article, StockCategory

        with test_app.session_factory() as db:
            category = StockCategory(name="covers", bucket_name="covers")
            db.add(category)
            db.flush()
            db_article = db.get(Article, article_id)
            db_article.stock_category_id = category.id
            db.commit()

        image_insert_called = False

        def fake_maybe_insert_images(*args, **kwargs):
            nonlocal image_insert_called
            image_insert_called = True
            return args[0], 0

        monkeypatch.setattr(
            "server.app.modules.articles.ai_format._call_litellm_completion",
            lambda **_: _fake_completion('{"heading_indices": [0], "image_positions": [0]}'),
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
